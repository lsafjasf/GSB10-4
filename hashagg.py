# -*- coding: utf-8 -*-
"""
hashagg.py —— 分组聚合的哈希溢出处理（外部内存 hash group-by）

只用 Python 3 标准库。

核心不变量
----------
对同一级溢出，分区数 K 固定，且分区号只由 key 决定：

    pid(key) = h(key) & (K - 1)        # K 为 2 的幂

一个 key 的全部行在溢出时必然写入同一个子分区文件；子分区再溢出时
同理继续按 key 哈希，因此每个 key 永远只落在一棵分区子树中，最终
只在一个叶子分区里被完整聚合，结果无需跨分区合并，同一组不可能
落到两个分区。

支持算子：count / sum / min / max（行的第 0 列为 key，其余列按 ops 指定）。
"""

from __future__ import annotations

import math
import os
import pickle
import shutil
import struct
import tempfile
import zlib
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

Row = tuple
Pair = Tuple[object, list]          # (key, state)

_U64_MASK = (1 << 64) - 1
_U32_MASK = (1 << 32) - 1


def _mix64(x: int) -> int:
    """splitmix64，打散连续整数 key，进程间确定（不依赖 PYTHONHASHSEED）。"""
    x &= _U64_MASK
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9 & _U64_MASK
    x = (x ^ (x >> 27)) * 0x94D049BB133111EB & _U64_MASK
    return x ^ (x >> 31)


def key_hash(key) -> int:
    """确定性 32 位哈希。int 走 splitmix64 快路径，其余走 crc32(repr())。"""
    if isinstance(key, int) and not isinstance(key, bool):
        return _mix64(key) & _U32_MASK
    if isinstance(key, bytes):
        return zlib.crc32(key)
    return zlib.crc32(repr(key).encode("utf-8", "surrogatepass"))


_GOLDEN = 0x9E3779B97F4A7C15


def partition_id(key, depth: int, mask: int) -> int:
    """第 depth 层溢出的分区号。

    关键：每一层用不同的盐重新混合哈希值。若各层都取同一批哈希低位，
    同一子分区内的 key 低位完全相同，再次分区会全部挤进同一个孙子分区，
    递归永不收敛。按深度加盐后，每层都是一次独立随机重划。
    同一 (key, depth) 分区号恒定 -> 同一组绝不落入两个分区。
    """
    return _mix64(key_hash(key) + _GOLDEN * (depth + 1)) & mask


def _next_pow2(x: int) -> int:
    return 1 if x <= 1 else 1 << (x - 1).bit_length()


# ----------------------------- 聚合算子定义 ----------------------------- #
# state 是与 ops 等长的 list。

def _op_init(op: str):
    if op in ("count", "sum"):
        return 0
    if op in ("min", "max"):
        return None
    raise ValueError("unknown op: %r" % (op,))


def _op_update(op: str, state, value):
    if op == "count":
        return state + 1
    if op == "sum":
        return state + value
    if op == "min":
        return value if state is None else min(state, value)
    if op == "max":
        return value if state is None else max(state, value)
    raise ValueError("unknown op: %r" % (op,))


def _op_merge(op: str, a, b):
    if op in ("count", "sum"):
        return a + b
    if op == "min":
        if a is None:
            return b
        if b is None:
            return a
        return min(a, b)
    if op == "max":
        if a is None:
            return b
        if b is None:
            return a
        return max(a, b)
    raise ValueError("unknown op: %r" % (op,))


class SpillAggregator:
    """
    流式分组聚合器，哈希表超过内存预算时递归分区落盘。

    参数
    ----
    ops:        [("count", None), ("sum", 1), ("min", 2), ("max", 2)]
                每个元素为 (算子名, 取列下标)，count 的列忽略
    budget:     内存中哈希表的估算字节数上限（字节）
    spill_dir:  溢出文件目录（默认临时目录，close() 时清理）
    fill_ratio: 分区目标装载率，fanout = ceil(估算总表大小 / (budget*fill))
    min_fanout / max_fanout: 一次溢出允许的最小/最大分区数（2 的幂）
    hot_cache:  溢出流式写盘时为热点 key 保留的小缓存项数，命中即就地聚合，
                倾斜数据下单个超大 key 不会把行原样刷满磁盘（内存占用有界）
    max_depth:  递归溢出深度保护（理论上不可能触达，触达即报错）
    """

    def __init__(
        self,
        ops: Sequence[Tuple[str, Optional[int]]],
        budget: int = 32 * 1024 * 1024,
        spill_dir: Optional[str] = None,
        fill_ratio: float = 0.75,
        min_fanout: int = 8,
        max_fanout: int = 128,
        hot_cache: int = 65536,
        max_depth: int = 32,
    ):
        if not (0.0 < fill_ratio < 1.0):
            raise ValueError("fill_ratio must be in (0, 1)")
        if max_fanout > 4096:
            raise ValueError("max_fanout too large (fd / buffer cost)")
        self.ops = list(ops)
        self.budget = int(budget)
        self.fill_ratio = fill_ratio
        self.min_fanout = int(min_fanout)
        self.max_fanout = int(max_fanout)
        self.hot_cache = int(hot_cache)
        self.max_depth = int(max_depth)
        # 热点缓存实际容量：不超过预算的 ~1/4（按 ~256B/项估算）
        self._hot_cap = min(self.hot_cache,
                            max(16, self.budget // 1024))
        self._owns_dir = spill_dir is None
        self.spill_dir = spill_dir or tempfile.mkdtemp(prefix="hashagg_")
        os.makedirs(self.spill_dir, exist_ok=True)
        self.stats = {
            "spills": [],          # 每次溢出事件
            "max_depth": 0,        # 最深溢出层级（根=0）
            "leaf_partitions": 0,  # 最终在内存中完成聚合的叶子分区数
            "spill_bytes": 0,      # 累计落盘字节
            "spill_pairs": 0,      # 累计落盘 (key,state) 条数
        }

    # ------------------------------ 生命周期 ------------------------------ #

    def close(self):
        if self._owns_dir and os.path.isdir(self.spill_dir):
            shutil.rmtree(self.spill_dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ------------------------------ 状态操作 ------------------------------ #

    def _row_state(self, row: Row) -> list:
        return [_op_update(op, _op_init(op), None if col is None else row[col])
                for op, col in self.ops]

    @staticmethod
    def _merge_into(dst: list, src: list, ops) -> None:
        for i, (op, _) in enumerate(ops):
            dst[i] = _op_merge(op, dst[i], src[i])

    def _entry_cost(self, key) -> int:
        # dict 槽 + key 对象 + state list 的保守估算（字节）
        if isinstance(key, int) and not isinstance(key, bool):
            ks = 33
        elif isinstance(key, str):
            ks = 50 + 2 * len(key)
        elif isinstance(key, bytes):
            ks = 34 + len(key)
        else:
            ks = 64
        return 90 + ks + 24 * len(self.ops)

    def _choose_fanout(self, est_total_bytes: int, depth: int) -> int:
        need = int(math.ceil(est_total_bytes / (self.budget * self.fill_ratio)))
        fanout = _next_pow2(max(self.min_fanout, need))
        return max(self.min_fanout, min(fanout, self.max_fanout))

    # ------------------------------ 对外入口 ------------------------------ #

    def aggregate(self, rows: Iterable[Row],
                  est_total_bytes: Optional[int] = None
                  ) -> Iterator[Tuple[object, tuple]]:
        """
        输入：行迭代器，每行 row[0] 为 key，其余列按 ops 取用。
        输出：(key, 聚合结果 tuple) 迭代器，结果按分区流式产出（顺序不定）。
        必须把迭代器消费完（或调用 close），以保证溢出文件被清理。
        """
        if est_total_bytes is None:
            try:
                # 有界长度输入：按全部不同 key 给上界，宁多分区不少分
                est_total_bytes = len(rows) * self._entry_cost(0)
            except TypeError:
                est_total_bytes = None
        pairs = ((row[0], self._row_state(row)) for row in rows)
        for key, state in self._process(pairs, depth=0,
                                       est_total=est_total_bytes):
            yield key, tuple(state)

    def collect(self, rows: Iterable[Row],
                est_total_bytes: Optional[int] = None
                ) -> dict:
        """消费完整结果为 dict（注意结果本身可能很大，仅自测/小规模使用）。"""
        return {key: result for key, result in
                self.aggregate(rows, est_total_bytes)}

    # ------------------------------ 核心递归 ------------------------------ #

    def _process(self, pairs: Iterator[Pair], depth: int,
                 est_total: Optional[int]) -> Iterator[Pair]:
        table: dict = {}
        est = 0
        it = iter(pairs)
        overflowed = False
        avg_entry = self._entry_cost(0)

        for key, state in it:
            cur = table.get(key)
            if cur is None:
                table[key] = list(state)
                est += self._entry_cost(key)
                if est > self.budget:
                    avg_entry = est / max(1, len(table))
                    overflowed = True
                    break
            else:
                self._merge_into(cur, state, self.ops)

        if not overflowed:
            # 本分区全部装入内存，成为叶子
            self.stats["max_depth"] = max(self.stats["max_depth"], depth)
            self.stats["leaf_partitions"] += 1
            return iter(list(table.items()))

        if depth >= self.max_depth:
            raise RuntimeError("spill recursion did not converge at depth %d"
                               % depth)

        # 估算完整输入的哈希表大小：已加载部分 + 剩余条数按均摊 key 开销（上界）
        loaded_keys = len(table)
        if est_total is None:
            est_total = int(est * 2)
        event = {"depth": depth, "loaded_keys": loaded_keys}

        fanout = self._choose_fanout(est_total, depth)
        mask = fanout - 1
        children = [_SpillFile(self.spill_dir, depth, i, fanout)
                    for i in range(fanout)]

        # 1) 已在内存表里的部分聚合状态落盘（记录这批状态的字节量，
        #    用来标定 spill 字节 -> 哈希表字节的膨胀率）
        bytes_before = sum(c.nbytes for c in children)
        for key, st in table.items():
            children[partition_id(key, depth, mask)].write(key, st)
        table_bytes = sum(c.nbytes for c in children) - bytes_before
        table = None

        # 2) 剩余流：热点 key 小缓存就地聚合（倾斜自适应），其余按 key 分区
        cache: dict = {}
        streamed = 0
        cap = self._hot_cap
        for key, state in it:
            streamed += 1
            cur = cache.get(key)
            if cur is not None:
                self._merge_into(cur, state, self.ops)
            elif len(cache) < cap:
                cache[key] = list(state)
            else:
                children[partition_id(key, depth, mask)].write(key, state)
        for key, st in cache.items():
            children[partition_id(key, depth, mask)].write(key, st)
        for ch in children:
            ch.close()

        rows_per = [ch.pairs for ch in children]
        bytes_per = [ch.nbytes for ch in children]
        event.update({
            "fanout": fanout,
            "est_total_bytes": int(est_total),
            "streamed_pairs": streamed,
            "rows_per_partition": rows_per,
            "bytes_per_partition": bytes_per,
        })
        self.stats["spills"].append(event)
        self.stats["spill_bytes"] += sum(bytes_per)
        self.stats["spill_pairs"] += sum(rows_per)

        # spill 字节 -> 表字节的实测比率（+25% 余量），各子分区按文件大小估计
        ratio = (est / table_bytes) if table_bytes > 0 else 4.0
        ratio = min(20.0, max(1.5, ratio * 1.25))
        # 3) 递归处理每个子分区；同一个 key 只可能出现在其中一个文件里
        def gen():
            for ch in children:
                child_est = int(ch.nbytes * ratio)
                for item in self._process(ch.read(), depth + 1, child_est):
                    yield item
                ch.unlink()

        return gen()


class _SpillFile:
    """
    溢出文件格式（连续 batch）：
        uint32 little-endian payload_len | pickle(list[(key, state)])
    长度前缀批处理减少系统调用；pickle 协议 4。
    batch 随 fanout 缩放，全部文件的常驻缓冲合计约 8MB。
    """

    HEADER = struct.Struct("<I")
    TOTAL_BUF_PAIRS = 1 << 17   # 全部溢出文件的缓冲条目合计上限

    def __init__(self, spill_dir: str, depth: int, idx: int, fanout: int = 8):
        self.path = os.path.join(spill_dir, "spill_d%d_p%d.bin" % (depth, idx))
        self.f = open(self.path, "wb", buffering=1 << 16)
        self.batch = max(256, min(4096, self.TOTAL_BUF_PAIRS // max(1, fanout)))
        self.buf: List[Pair] = []
        self.nbytes = 0
        self.pairs = 0

    def write(self, key, state) -> None:
        self.buf.append((key, state))
        if len(self.buf) >= self.batch:
            self._flush()

    def _flush(self) -> None:
        if not self.buf:
            return
        payload = pickle.dumps(self.buf, protocol=4)
        self.f.write(self.HEADER.pack(len(payload)))
        self.f.write(payload)
        self.pairs += len(self.buf)
        self.nbytes += self.HEADER.size + len(payload)
        self.buf.clear()

    def close(self) -> None:
        self._flush()
        self.f.close()

    def read(self) -> Iterator[Pair]:
        with open(self.path, "rb", buffering=1 << 16) as f:
            hdr = self.HEADER
            while True:
                head = f.read(hdr.size)
                if not head:
                    return
                (plen,) = hdr.unpack(head)
                data = f.read(plen)
                if len(data) != plen:
                    raise IOError("truncated spill file: %s" % self.path)
                batch = pickle.loads(data)
                for pair in batch:
                    yield pair

    def unlink(self) -> None:
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass

"""spilldedup.py - 精确去重算子，带内存上界与磁盘溢写（仅标准库）。

算法（external sort，绝不使用哈希做相等判断）：
  1. add(key) 时把键规范化为“带类型标签的字节串”后放入内存缓冲；
     按每条记录的保守内存计费累计，达到上界就把缓冲排序、相邻去重、
     写成一个不可变溢写文件（run）。
  2. results() 时把最后一块内存数据与所有 run 做 K 路归并（heapq.merge），
     归并流中相邻相等的键只保留一条，结果按规范化字节序升序输出。
正确性：
  - 相等判定只依赖规范化字节串的字节比较；任何哈希碰撞都不可能导致误去重。
  - 溢写/归并不改变多重集合的“去重结果”：分趟是对输入的一次划分，
    归并排序后按相邻比较去重，与一次性在内存 set 去重的结果集合完全相同。

键类型：bytes / str / int / bool（bool 与 int 分开，语义同 Python set）。
"""

from __future__ import annotations

import heapq
import os
import struct
import sys
import tempfile
import weakref

__all__ = ["SpillDedup", "dedup", "encode_key", "process_rss_bytes",
           "memory_limit_for_budget"]

_MAGIC = b"SDDRv1\n"
_LEN = struct.Struct(">Q")
_WRITE_CHUNK = 1 << 20          # 1 MiB 批量写缓冲
_READ_CHUNK = 1 << 13           # 8 KiB 每个归并 reader 的读缓冲

# 单条缓冲记录的保守固定开销（bytes 对象头与长度在 charge 中另算）。
#   list 指针 8B + 预留槽/整体分配 ≤8B + dict/set 化余量 48B = 64B
_ITEM_FIXED = 64
_BYTES_OVERHEAD = sys.getsizeof(b"")   # CPython 64 位下为 33


def encode_key(key) -> bytes:
    """把键规范化为带类型标签的字节串。

    - 不同类型前缀不同，跨类型绝不相等；
    - str 用 UTF-8；int 用规范十进制（无 '+01' 之类二义）；
    - 标签字节取在数字/普通字节之外，保证字节比较与相等判定一致。
    """
    if isinstance(key, bool):
        return b"b\x01" if key else b"b\x00"
    if isinstance(key, int):
        return b"i" + str(key).encode("ascii")
    if isinstance(key, str):
        return b"s" + key.encode("utf-8")
    if isinstance(key, (bytes, bytearray, memoryview)):
        return b"x" + bytes(key)
    raise TypeError("unsupported key type: %r" % type(key).__name__)


def _charge(encoded: bytes) -> int:
    """一条已编码记录在缓冲期占用的保守字节数。"""
    return sys.getsizeof(encoded) + _ITEM_FIXED


def process_rss_bytes() -> int:
    """当前进程常驻内存（字节）。Linux 读 /proc，其他平台返回 0。"""
    try:
        with open("/proc/self/status", "rb") as fh:
            for line in fh:
                if line.startswith(b"VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _starttime_ticks(pid: int):
    """进程启动时间（clock ticks），用于识别 PID 复用；失败返回 None。"""
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            data = fh.read()
        # comm 里可能含空格/括号，取最后一个 ')' 之后切分
        rest = data[data.rfind(b")") + 2:].split()
        return rest[19]          # 第 22 个字段（0-based: 19）
    except OSError:
        return None


def memory_limit_for_budget(total_budget_bytes: int,
                            safety_bytes: int = 2 << 20) -> int:
    """把“进程总 RSS 预算”换算成算子缓冲上限：预算 - 当前 RSS - 安全余量。"""
    rss = process_rss_bytes()
    limit = total_budget_bytes - (rss or 12 << 20) - safety_bytes
    if limit < (1 << 20):
        raise ValueError(
            "预算太小：当前 RSS=%d 字节，无法在 %d 字节预算下留出缓冲"
            % (rss, total_budget_bytes))
    return limit


class _RunReader:
    """顺序读取一个溢写 run，每次产出一条 bytes。"""

    __slots__ = ("_fh", "_buf", "_eof")

    def __init__(self, path: str):
        self._fh = open(path, "rb", buffering=_READ_CHUNK)
        if self._fh.read(len(_MAGIC)) != _MAGIC:
            raise ValueError("坏的溢写文件: %s" % path)
        self._buf = None
        self._eof = False

    def read_one(self):
        if self._eof:
            return None
        header = self._fh.read(_LEN.size)
        if not header:
            self._eof = True
            return None
        if len(header) != _LEN.size:
            raise ValueError("溢写文件长度头不完整")
        (size,) = _LEN.unpack(header)
        payload = self._fh.read(size)
        if len(payload) != size:
            raise ValueError("溢写文件内容不完整")
        return payload

    def close(self):
        self._fh.close()


def _sorted_unique(encoded_list):
    """排序并相邻去重，返回新列表。"""
    encoded_list.sort()
    out = []
    prev = None
    have = False
    for item in encoded_list:
        if not have or item != prev:
            out.append(item)
            prev = item
            have = True
    return out


class SpillDedup:
    """内存受控的精确去重算子。

    参数
    ----
    memory_limit_bytes : int | None
        缓冲内存计费上限（字节）。达到后立即溢写一个 run。None 表示不限内存。
    spill_dir : str | None
        溢写根目录，默认使用系统临时目录下的 spilldedup/。
        每个实例使用独立子目录；构造时会清扫“属主进程已死”的残留目录。

    工作集内存上界（不含 Python 解释器基线）：
        <= memory_limit_bytes
         + 1 MiB（溢写批量写缓冲）
         + L_max（单条键最大计费，溢写触发瞬间的越过量）
         + 8 KiB * K（归并时各 run 的读缓冲，K 为 run 数）
    缓冲计费对每条记录保守多收（见 _charge），故实际值稳定低于该式。
    """

    _counter = 0

    def __init__(self, memory_limit_bytes=None, spill_dir=None):
        if memory_limit_bytes is not None and memory_limit_bytes <= 0:
            raise ValueError("memory_limit_bytes 必须为正整数或 None")
        self.memory_limit_bytes = memory_limit_bytes
        self._base = spill_dir or os.path.join(
            tempfile.gettempdir(), "spilldedup")
        os.makedirs(self._base, exist_ok=True)
        self._sweep_stale()

        SpillDedup._counter += 1
        ticks = _starttime_ticks(os.getpid()) or b"-"
        name = "p%d_t%s_%d" % (os.getpid(),
                               ticks.decode("ascii", "replace"),
                               SpillDedup._counter)
        self.dir = os.path.join(self._base, name)
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "owner"), "w") as fh:
            fh.write("pid=%d\nstarttime=%s\n" %
                     (os.getpid(), ticks.decode("ascii", "replace")))

        self._buf = []
        self._charged = 0
        self._runs = []            # 已发布 run 的绝对路径
        self.spill_count = 0
        self.spill_bytes_total = 0
        self.keys_seen = 0
        self._closed = False
        self._finalizer = weakref.finalize(self, SpillDedup._cleanup_dir,
                                           self.dir)

    # ------------------------------------------------------------------
    # 残留清理
    # ------------------------------------------------------------------
    def _sweep_stale(self):
        """删除属主进程已经不存在（或 PID 已被复用）的实例目录。"""
        try:
            names = os.listdir(self._base)
        except OSError:
            return
        for name in names:
            path = os.path.join(self._base, name)
            owner = os.path.join(path, "owner")
            if not os.path.isfile(owner):
                continue
            pid = None
            starttime = None
            try:
                with open(owner, "r") as fh:
                    for line in fh:
                        if line.startswith("pid="):
                            pid = int(line[4:].strip())
                        elif line.startswith("starttime="):
                            starttime = line[10:].strip().encode()
            except (OSError, ValueError):
                continue
            if pid is None:
                continue
            alive_ticks = _starttime_ticks(pid)
            # 进程存在且启动时间一致才算存活；否则（死亡 / PID 复用）清扫
            if alive_ticks is not None and (
                    starttime in (None, b"-") or alive_ticks == starttime):
                continue
            self._rmtree(path)

    @staticmethod
    def _rmtree(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    @classmethod
    def _cleanup_dir(cls, path):
        cls._rmtree(path)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def add(self, key) -> None:
        if self._closed:
            raise RuntimeError("算子已关闭")
        encoded = encode_key(key)
        self._buf.append(encoded)
        self._charged += _charge(encoded)
        self.keys_seen += 1
        if (self.memory_limit_bytes is not None
                and self._charged >= self.memory_limit_bytes):
            self._spill()

    def add_many(self, iterable) -> None:
        for key in iterable:
            self.add(key)

    def _spill(self) -> None:
        """触发点：缓冲计费达到 memory_limit_bytes。排序去重后落盘一个 run。"""
        if not self._buf:
            return
        unique = _sorted_unique(self._buf)
        self._buf.clear()
        self._charged = 0

        self.spill_count += 1
        final_path = os.path.join(self.dir, "run-%06d.bin" % self.spill_count)
        tmp_path = final_path + ".tmp"
        total = 0
        chunk = bytearray()
        with open(tmp_path, "wb") as fh:
            chunk += _MAGIC
            for payload in unique:
                chunk += _LEN.pack(len(payload))
                chunk += payload
                if len(chunk) >= _WRITE_CHUNK:
                    fh.write(chunk)
                    total += len(chunk)
                    chunk.clear()
            if chunk:
                fh.write(chunk)
                total += len(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, final_path)          # 原子发布
        self._runs.append(final_path)
        self.spill_bytes_total += total

    # ------------------------------------------------------------------
    # 归并输出
    # ------------------------------------------------------------------
    def results(self):
        """返回一个迭代器：流式产出去重后的键（按规范化字节序升序）。

        调用后算子关闭；迭代结束（或迭代器被 close/回收）时自动清理
        全部溢写文件。只能有效调用一次。
        """
        if self._closed:
            return iter(())
        self._closed = True
        return self._result_iter()

    def _result_iter(self):
        readers = []
        try:
            streams = []
            if self._buf:
                # 最后一块内存数据：就地排序去重，作为第 K+1 路输入
                mem_run = _sorted_unique(self._buf)
                self._buf.clear()
                self._charged = 0
                streams.append(iter(mem_run))
            for path in self._runs:
                reader = _RunReader(path)
                readers.append(reader)
                streams.append(_iter_reader(reader))

            prev = None
            have = False
            for encoded in heapq.merge(*streams):
                if not have or encoded != prev:
                    prev = encoded
                    have = True
                    yield _decode_key(encoded)
        finally:
            for reader in readers:
                try:
                    reader.close()
                except OSError:
                    pass
            self._finalizer()
            self._runs.clear()

    # 上下文管理 / 析构
    def close(self):
        if not self._closed:
            self._closed = True
            self._finalizer()
            self._runs.clear()
            self._buf = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _iter_reader(reader: _RunReader):
    while True:
        item = reader.read_one()
        if item is None:
            return
        yield item


def _decode_key(encoded: bytes):
    tag, body = encoded[:1], encoded[1:]
    if tag == b"i":
        return int(body)
    if tag == b"s":
        return body.decode("utf-8")
    if tag == b"x":
        return body
    if tag == b"b":
        return body == b"\x01"
    raise ValueError("未知类型标签: %r" % tag)


def dedup(iterable, memory_limit_bytes=None, spill_dir=None):
    """便捷封装：流式去重，返回生成器。"""
    op = SpillDedup(memory_limit_bytes, spill_dir)
    op.add_many(iterable)
    yield from op.results()

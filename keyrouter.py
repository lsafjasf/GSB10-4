"""键路由一致性库（Python 3 标准库实现）。

核心组件
--------
- Router : 键 -> 分区的确定性路由表，使用 HRW（Rendezvous / Highest Random
           Weight）一致性哈希。分区数不变时同一键永远落同一分区；分区数变化时
           平均只需迁移 1/N 的键（N 为新分区数），迁移量最小。
- PartitionedLog : 内存版分区日志（模拟 Kafka 一类的分区存储）。
- Producer : 为每个键维护单调递增序号；迁移期支持双写（旧分区数 + 新分区数）。
- MergingConsumer : 双读两个分区布局，按 (键, 序号) 去重，并按序号重排后交付，
                    保证迁移前后同键消息严格有序。

迁移方案及理由见 README.md。
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any, Dict, List, Optional, Tuple

Message = Tuple[Any, int, Any]  # (key, seq, payload)

_EMPTY_MARKER = b"\x00empty-key\x00"


def _score(key_digest: bytes, partition: int) -> int:
    """键与候选分区的耦合权重：确定性、与键长/分区数无关。"""
    h = hashlib.blake2b(
        key_digest + partition.to_bytes(4, "big"), digest_size=8
    )
    return int.from_bytes(h.digest(), "big")


def _key_digest(key: Any) -> bytes:
    if isinstance(key, bytes):
        data = key
    elif isinstance(key, str):
        data = key.encode("utf-8")
    elif isinstance(key, int):
        data = key.to_bytes(8, "big", signed=True)
    else:
        data = repr(key).encode("utf-8")
    return hashlib.blake2b(data, digest_size=8).digest()


def _is_empty_key(key: Any) -> bool:
    return key is None or key == "" or key == b""


class Router:
    """键 -> 分区路由器。

    映射规则（HRW）：
        digest(k)   = BLAKE2b(k, 8 字节)
        score(k, p) = BLAKE2b(digest(k) || uint32be(p), 8 字节) 解释为无符号整数
        partition(k)= argmax_p score(k, p),  p = 0..N-1

    - N（分区数）不变时，同一键的结果是纯函数，跨进程、跨实例、跨时间稳定。
    - 不使用 Python 内建 hash()（PYTHONHASHSEED 随机化，跨进程不稳定）。
    - 结果在分区上近似均匀：每个分区对任意键都有 1/N 的概率胜出。
    - 空键（None / "" / b""）无身份、无法保证顺序，采用确定性轮转摊薄，
      不集中压在单一分区；非空键不享受此待遇，热点键仍固定在一个分区
      （顺序语义优先于负载均衡，见 README）。
    """

    def __init__(self, num_partitions: int) -> None:
        if num_partitions < 1:
            raise ValueError("num_partitions 必须 >= 1")
        self._num_partitions = num_partitions
        self._cache: Dict[bytes, int] = {}
        self._empty_counter = 0
        self._lock = threading.Lock()

    @property
    def num_partitions(self) -> int:
        return self._num_partitions

    def partition_for(self, key: Any) -> int:
        if _is_empty_key(key):
            return self._route_empty_key()
        digest = _key_digest(key)
        hit = self._cache.get(digest)
        if hit is not None:
            return hit
        best_partition = 0
        best_score = -1
        for partition in range(self._num_partitions):
            s = _score(digest, partition)
            if s > best_score:
                best_score = s
                best_partition = partition
        self._cache[digest] = best_partition
        return best_partition

    def _route_empty_key(self) -> int:
        # 确定性轮转：按路由调用顺序 counter % N 摊到各分区。
        # 空键不存在“同一键稳定落同一分区”的语义（它本身不携带身份）。
        with self._lock:
            partition = self._empty_counter % self._num_partitions
            self._empty_counter += 1
        return partition

    def clear_cache(self) -> None:
        """清空分配缓存（基准测试冷缓存场景用）。"""
        self._cache.clear()


class PartitionedLog:
    """内存分区日志，append 保持分区内严格 FIFO。"""

    def __init__(self, num_partitions: int) -> None:
        self._partitions: List[List[Message]] = [
            [] for _ in range(num_partitions)
        ]

    @property
    def num_partitions(self) -> int:
        return len(self._partitions)

    def append(self, partition: int, message: Message) -> None:
        self._partitions[partition].append(message)

    def partition(self, partition: int) -> List[Message]:
        return self._partitions[partition]

    def total_messages(self) -> int:
        return sum(len(p) for p in self._partitions)


class Producer:
    """带每键单调序号的生产者，支持 old / dual / new 三种写入模式。

    迁移时序：
      1. old  : 只按旧分区数写 old_log
      2. dual : 同时写 old_log（旧布局）和 new_log（新布局），双写
      3. new  : 只按新分区数写 new_log（old_log 已确认全部消费完后才能切换）
    """

    OLD = "old"
    DUAL = "dual"
    NEW = "new"

    def __init__(
        self,
        old_router: Router,
        new_router: Router,
        old_log: PartitionedLog,
        new_log: PartitionedLog,
    ) -> None:
        self._old_router = old_router
        self._new_router = new_router
        self._old_log = old_log
        self._new_log = new_log
        self._seq: Dict[Any, int] = {}
        self._sent: Dict[Any, List[Any]] = {}
        self.mode = self.OLD

    def send(self, key: Any, payload: Any = None) -> Message:
        seq = self._seq.get(key, 0)
        self._seq[key] = seq + 1
        message = (key, seq, payload)
        self._sent.setdefault(key, []).append(payload)
        if self.mode in (self.OLD, self.DUAL):
            self._old_log.append(self._old_router.partition_for(key), message)
        if self.mode in (self.DUAL, self.NEW):
            self._new_log.append(self._new_router.partition_for(key), message)
        return message

    def sent_payloads(self, key: Any) -> List[Any]:
        return list(self._sent.get(key, []))

    def totals_by_key(self) -> Dict[Any, int]:
        return {key: len(payloads) for key, payloads in self._sent.items()}


class MergingConsumer:
    """双读去重 + 按序号重排的消费者。

    同时轮询 old_log / new_log；同一条消息在双写期会在两个日志各出现一次，
    按 (键, 序号) 去重；每个键只允许交付“下一个期望序号”，未到的先进缓冲区，
    因此即便两个日志的到达顺序交错，交付顺序严格等于序号顺序。
    """

    def __init__(self) -> None:
        self._offsets: Dict[int, List[int]] = {}
        self._seen: set = set()
        self._next_seq: Dict[Any, int] = {}
        self._buffer: Dict[Any, Dict[int, Any]] = {}
        self.delivered: Dict[Any, List[Any]] = {}
        self.delivered_seqs: Dict[Any, List[int]] = {}

    def poll(self, *logs: PartitionedLog) -> int:
        newly_delivered = 0
        for log in logs:
            offsets = self._offsets.setdefault(
                id(log), [0] * log.num_partitions
            )
            for partition in range(log.num_partitions):
                part = log.partition(partition)
                while offsets[partition] < len(part):
                    key, seq, payload = part[offsets[partition]]
                    offsets[partition] += 1
                    ident = (key, seq)
                    if ident in self._seen:
                        continue
                    self._seen.add(ident)
                    self._buffer.setdefault(key, {})[seq] = payload

        for key, buf in self._buffer.items():
            nxt = self._next_seq.get(key, 0)
            while nxt in buf:
                payload = buf.pop(nxt)
                self.delivered.setdefault(key, []).append(payload)
                self.delivered_seqs.setdefault(key, []).append(nxt)
                nxt += 1
                newly_delivered += 1
            self._next_seq[key] = nxt
        return newly_delivered

    def consumed_all(self, log: PartitionedLog) -> bool:
        offsets = self._offsets.get(id(log))
        if offsets is None:
            return False
        return all(
            off == len(log.partition(part))
            for part, off in enumerate(offsets)
        )

    def order_report(self) -> List[Tuple[Any, int, int, int, bool]]:
        """返回 (键, 条数, 首序号, 末序号, 是否严格 0..n-1) 的对拍报告。"""
        report = []
        for key, seqs in sorted(
            self.delivered_seqs.items(), key=lambda kv: repr(kv[0])
        ):
            ok = seqs == list(range(len(seqs)))
            report.append(
                (key, len(seqs), seqs[0] if seqs else -1, seqs[-1] if seqs else -1, ok)
            )
        return report

"""环形缓冲区：读写指针管理（仅标准库）。

指针 / 游标比较规则（核心不变式）
================================

1. 维护两个**单调递增、永不回绕**的逻辑游标 ``read_pos`` / ``write_pos``；
   物理槽位 = 逻辑游标 % capacity。
2. 占用量 ``size = write_pos - read_pos``，恒满足 ``0 <= size <= capacity``。
3. 空  <=>  size == 0（即 write_pos == read_pos）；
   满  <=>  size == capacity。
   —— 绝不用“物理槽位相等”判断空/满：回绕后 read_slot == write_slot
      既可能是空也可能是满，这正是环绕处最容易写错的地方。
4. 可读字节数 = size；可写字节数 = capacity - size。
5. 写入量被 clamp 到 ``capacity - size``，即 write_pos 永远不会越过
   ``read_pos + capacity``，因此任何情况下都不会覆盖未读数据。
6. 一次读/写物理上最多拆成两段：[slot, capacity) 与 [0, remainder)，
   ``write_segments`` / ``read_segments`` 直接暴露分段结果。

线程安全：内部一把锁 + 条件变量，阻塞接口可用于生产/消费并发场景。
"""

from __future__ import annotations

import threading
import time


class RingBuffer:
    """定长环形字节缓冲区。"""

    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._buf = bytearray(capacity)
        self._cap = capacity
        self._read_pos = 0   # 逻辑读游标，单调递增
        self._write_pos = 0  # 逻辑写游标，单调递增
        self._cond = threading.Condition()

    # ---------- 状态查询（调用方需自行保证并发场景下的语义） ----------

    @property
    def capacity(self) -> int:
        return self._cap

    @property
    def size(self) -> int:
        """已占用字节数 = write_pos - read_pos。"""
        return self._write_pos - self._read_pos

    @property
    def free(self) -> int:
        """剩余可写字节数 = capacity - size。"""
        return self._cap - self.size

    def is_empty(self) -> bool:
        return self.size == 0

    def is_full(self) -> bool:
        return self.size == self._cap

    # ---------- 非阻塞读写 ----------

    def _write_unlocked(self, data) -> int:
        """写入尽可能多的字节，返回实际写入数。调用方须持锁。"""
        n = min(len(data), self._cap - (self._write_pos - self._read_pos))
        if n <= 0:
            return 0
        start = self._write_pos % self._cap
        first = min(n, self._cap - start)          # 第一段：[start, cap)
        self._buf[start:start + first] = data[:first]
        if first < n:                              # 第二段（回绕）：[0, n-first)
            self._buf[0:n - first] = data[first:n]
        self._write_pos += n
        return n

    def _read_segments_unlocked(self, n: int):
        """读出至多 n 字节，按物理连续段返回 bytes 列表（1~2 段）。"""
        n = min(n, self._write_pos - self._read_pos)
        if n <= 0:
            return []
        start = self._read_pos % self._cap
        first = min(n, self._cap - start)
        segments = [bytes(self._buf[start:start + first])]
        if first < n:                              # 回绕，补第二段
            segments.append(bytes(self._buf[0:n - first]))
        self._read_pos += n
        return segments

    def write(self, data) -> int:
        """非阻塞写，返回实际写入字节数（可能小于 len(data)，满时为 0）。"""
        with self._cond:
            n = self._write_unlocked(data)
            if n:
                self._cond.notify_all()
            return n

    def read(self, n: int) -> bytes:
        """非阻塞读，返回至多 n 字节（空时返回 b\"\"）。"""
        with self._cond:
            segments = self._read_segments_unlocked(n)
            if segments:
                self._cond.notify_all()
            return b"".join(segments)

    # ---------- 分段读写（一次操作跨回绕边界时返回多段） ----------

    def write_segments(self, data):
        """非阻塞写，返回实际写入的物理分段列表 [(slot_offset, length), ...]。"""
        with self._cond:
            base = self._write_pos
            n = self._write_unlocked(data)
            if n:
                self._cond.notify_all()
            if n <= 0:
                return []
            start = base % self._cap
            first = min(n, self._cap - start)
            segments = [(start, first)]
            if first < n:
                segments.append((0, n - first))
            return segments

    def read_segments(self, n: int):
        """非阻塞读，返回物理分段列表 [bytes, ...]（回绕时为 2 段）。"""
        with self._cond:
            segments = self._read_segments_unlocked(n)
            if segments:
                self._cond.notify_all()
            return segments

    # ---------- 阻塞读写（生产/消费并发用） ----------

    def write_blocking(self, data, timeout: float | None = None) -> int:
        """阻塞直到把 data 全部写入（或超时），返回已写字节数。"""
        view = memoryview(data)
        sent = 0
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while sent < len(view):
                n = self._write_unlocked(view[sent:])
                sent += n
                if n:
                    # 每写入一段就通知：否则写满后等待空间时，
                    # 消费者可能永远在等“其实已经到达”的数据（死锁）。
                    self._cond.notify_all()
                if sent >= len(view):
                    break
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._cond.wait(remaining)
            return sent

    def read_blocking(self, n: int, timeout: float | None = None) -> bytes:
        """阻塞直到至少读到 1 字节（或超时返回 b\"\"），最多返回 n 字节。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while True:
                segments = self._read_segments_unlocked(n)
                if segments:
                    self._cond.notify_all()
                    return b"".join(segments)
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return b""
                self._cond.wait(remaining)

    # ---------- 调试 / 校验 ----------

    def check_invariant(self) -> bool:
        """校验核心不变式，供测试使用。"""
        size = self._write_pos - self._read_pos
        return (
            0 <= size <= self._cap
            and self._read_pos >= 0
            and self._write_pos >= self._read_pos
        )

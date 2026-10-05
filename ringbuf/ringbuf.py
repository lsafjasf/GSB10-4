"""环形缓冲区（ring buffer）库 —— 仅使用 Python 标准库。

指针 / 游标比较规则（核心约定）
================================

缓冲区容量为 C（即可读字节上限），内部存储为长度恰好 C 的 bytearray。

- ``_r`` 读游标：最旧未读字节所在的下标，取值范围 [0, C)。
- ``_w`` 写游标：下一个可写下标，取值范围 [0, C)。
- 两个游标只按 ``mod C`` 推进，**绝不直接比较大小**（``_r < _w`` 之类的
  比较在回绕后没有意义，是本库禁止的写法）。
- ``_n`` 可读字节数，取值范围 [0, C]，是唯一的状态判据：

  - 空：``_n == 0``（此时必然 ``_r == _w``）
  - 满：``_n == C``（此时同样 ``_r == _w``）
  - 因此 ``_r == _w`` 本身有歧义，必须配合 ``_n`` 区分空 / 满。
    本实现用计数器 ``_n`` 消歧（另一种常见方案是牺牲一个槽位，
    本库不采用，以便 C 个字节全部可用）。

- 连续段长度由 ``_n`` 与 ``C - 游标`` 推出，例如一次读 n 字节时：
  第一段长度 ``k = min(n, C - _r)``，第二段长度 ``n - k``（回绕段，
  可能为 0）。写侧同理，把 ``_r`` 换成 ``_w``、把 n 换成可写长度。

不变量（每次公开操作前后均成立，selftest 中会断言）：
  0 <= _r < C, 0 <= _w < C, 0 <= _n <= C,
  (_r + _n) % C == _w
"""

from __future__ import annotations

import threading


class BufferFull(Exception):
    """缓冲区满，无法再写入。"""


class BufferEmpty(Exception):
    """缓冲区空，无数据可读。"""


class RingBuffer:
    """定容环形字节缓冲区。非线程安全；并发场景请用 BlockingRingBuffer。"""

    __slots__ = ("_buf", "_r", "_w", "_n")

    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._buf = bytearray(capacity)
        self._r = 0  # 读游标
        self._w = 0  # 写游标
        self._n = 0  # 可读字节数（空/满判据）

    # ---- 状态查询 ------------------------------------------------------

    @property
    def capacity(self) -> int:
        return len(self._buf)

    def __len__(self) -> int:
        """可读字节数。"""
        return self._n

    @property
    def free(self) -> int:
        """可写字节数。"""
        return self.capacity - self._n

    @property
    def is_empty(self) -> bool:
        return self._n == 0

    @property
    def is_full(self) -> bool:
        return self._n == self.capacity

    def check_invariants(self) -> None:
        c = self.capacity
        assert 0 <= self._r < c, f"read cursor out of range: {self._r}"
        assert 0 <= self._w < c, f"write cursor out of range: {self._w}"
        assert 0 <= self._n <= c, f"count out of range: {self._n}"
        assert (self._r + self._n) % c == self._w, (
            f"cursor/count mismatch: r={self._r} n={self._n} w={self._w}"
        )

    # ---- 分段视图 ------------------------------------------------------

    def read_segments(self, n: int | None = None) -> list[memoryview]:
        """按可读顺序返回前 n 个字节（默认全部）的连续段视图。

        不复制数据、不移动游标。未回绕时返回 1 段，回绕时返回 2 段，
        空缓冲区返回空列表。用于验证"一次读跨越多段"的情形。
        """
        if n is None:
            n = self._n
        if n < 0 or n > self._n:
            raise ValueError(f"cannot read {n} bytes, only {self._n} available")
        if n == 0:
            return []
        c = self.capacity
        first = min(n, c - self._r)
        segs = [memoryview(self._buf)[self._r : self._r + first]]
        if n > first:  # 回绕段
            segs.append(memoryview(self._buf)[: n - first])
        return segs

    # ---- 写入 ----------------------------------------------------------

    def write(self, data: bytes | bytearray | memoryview) -> int:
        """写入 data，返回实际写入字节数（满时写入 0，可能部分写入）。"""
        n = min(len(data), self.free)
        if n == 0:
            return 0
        c = self.capacity
        first = min(n, c - self._w)
        self._buf[self._w : self._w + first] = data[:first]
        if n > first:  # 回绕段
            self._buf[: n - first] = data[first:n]
        self._w = (self._w + n) % c
        self._n += n
        return n

    def write_all(self, data: bytes | bytearray | memoryview) -> None:
        """整段写入；空间不足时抛 BufferFull，一个字节都不写。"""
        if len(data) > self.free:
            raise BufferFull(
                f"need {len(data)} bytes, only {self.free} free"
            )
        self.write(data)

    # ---- 读出 ----------------------------------------------------------

    def read(self, n: int) -> bytes:
        """读取至多 n 字节（不足时返回现有全部），并推进读游标。"""
        n = min(n, self._n)
        if n == 0:
            return b""
        c = self.capacity
        first = min(n, c - self._r)
        out = bytes(self._buf[self._r : self._r + first])
        if n > first:  # 回绕段
            out += bytes(self._buf[: n - first])
        self._r = (self._r + n) % c
        self._n -= n
        return out

    def read_all(self) -> bytes:
        """读出全部可读字节。"""
        return self.read(self._n)

    def peek(self, n: int) -> bytes:
        """查看前 n 字节但不推进读游标。"""
        return b"".join(bytes(s) for s in self.read_segments(n))


class BlockingRingBuffer:
    """带条件变量的阻塞版环形缓冲区，用于单/多生产者-消费者场景。

    保证：满时生产者阻塞等待，**绝不覆盖未读数据**；
    空时消费者阻塞等待，绝不读到伪造数据。
    """

    def __init__(self, capacity: int):
        self._rb = RingBuffer(capacity)
        self._cond = threading.Condition()

    @property
    def capacity(self) -> int:
        return self._rb.capacity

    def put(self, data: bytes) -> None:
        """阻塞直到 data 作为**一整条消息**原子写入（len(data) <= capacity）。

        空间不足时整体等待，绝不留下半包，因此多个生产者的消息不会在
        缓冲区内部交错；配合定长 get 可直接做定长帧。
        """
        if len(data) > self._rb.capacity:
            raise ValueError("message larger than capacity")
        with self._cond:
            while self._rb.free < len(data):
                self._cond.wait()
            self._rb.write_all(data)
            self._cond.notify()  # 有新数据，唤醒一个消费者

    def get(self, n: int) -> bytes:
        """阻塞直到恰好读到 n 字节。"""
        if n > self._rb.capacity:
            raise ValueError("read length larger than capacity")
        parts: list[bytes] = []
        need = n
        with self._cond:
            while need > 0:
                chunk = self._rb.read(need)
                if chunk:
                    parts.append(chunk)
                    need -= len(chunk)
                    self._cond.notify()  # 腾出空间，唤醒一个生产者
                else:
                    self._cond.wait()
        return b"".join(parts)

    def __len__(self) -> int:
        with self._cond:
            return len(self._rb)

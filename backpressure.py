"""Streaming backpressure with high/low watermark hysteresis (stdlib only).

Water level rules
-----------------
- Frames are atomic: a frame is appended to / removed from the buffer as a
  whole, in FIFO order, exactly once. Frames are never split, dropped or
  duplicated, so the consumer output is byte-identical to the producer input.
- PAUSE: after a write, if ``buffered_bytes >= high_watermark`` the stream
  flips to paused. The producer blocks inside the next ``write()`` call.
- RESUME: while paused, the consumer drains frames; once
  ``buffered_bytes <= low_watermark`` *and* at least ``min_pause_seconds``
  have elapsed since the pause started, the stream flips back to running
  and the producer is woken.
- The gap ``high - low`` (hysteresis) bounds the flip frequency to at most
  one pause/resume cycle per ``high - low`` bytes consumed.
  ``min_pause_seconds`` additionally bounds it to one cycle per that many
  seconds.

Memory bound (provable)
-----------------------
Invariant: a frame is appended only while not paused, and "not paused"
implies ``buffered_bytes < high_watermark``. Therefore immediately after
any append::

    buffered_bytes < high_watermark + len(last_frame)
                 <= high_watermark + max_frame_size

At most one extra frame is in flight on the producer side and one on the
consumer side, so total buffering memory is bounded by
``high_watermark + 3 * max_frame_size + O(1)`` — independent of the total
number of messages. A single oversized frame (larger than the high
watermark) is allowed through whole and cannot deadlock: it is delivered
intact, then the stream pauses until the buffer drains to the low
watermark.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Optional

__all__ = ["BackpressureStream"]


class BackpressureStream:
    """SPSC-friendly async byte-frame stream with watermark backpressure."""

    def __init__(
        self,
        high_watermark: int = 1 << 20,
        low_watermark: Optional[int] = None,
        min_pause_seconds: float = 0.0,
    ) -> None:
        if low_watermark is None:
            low_watermark = high_watermark // 2
        if not (0 <= low_watermark < high_watermark):
            raise ValueError("need 0 <= low_watermark < high_watermark")
        if min_pause_seconds < 0:
            raise ValueError("min_pause_seconds must be >= 0")
        self.high_watermark = high_watermark
        self.low_watermark = low_watermark
        self.min_pause_seconds = min_pause_seconds

        self._buf: deque[bytes] = deque()
        self._bytes = 0
        self._paused = False
        self._finished = False
        self._cond = asyncio.Condition()
        self._pause_started = 0.0
        self._resume_scheduled = False

        # Observable statistics (used by tests/benchmarks).
        self.pause_count = 0
        self.resume_count = 0
        self.max_buffered_bytes = 0
        self.frames_in = 0
        self.frames_out = 0
        self.bytes_in = 0
        self.bytes_out = 0

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def buffered_bytes(self) -> int:
        return self._bytes

    async def write(self, frame: bytes) -> None:
        """Append one frame; blocks while the stream is paused."""
        if self._finished:
            raise RuntimeError("write() after finish()")
        frame = bytes(frame)
        async with self._cond:
            while self._paused:
                await self._cond.wait()
            self._buf.append(frame)
            self._bytes += len(frame)
            self.frames_in += 1
            self.bytes_in += len(frame)
            if self._bytes > self.max_buffered_bytes:
                self.max_buffered_bytes = self._bytes
            if not self._paused and self._bytes >= self.high_watermark:
                self._paused = True
                self.pause_count += 1
                self._pause_started = time.monotonic()
            self._cond.notify_all()  # wake a consumer waiting for frames

    async def read(self) -> Optional[bytes]:
        """Pop the oldest frame; returns None at end-of-stream."""
        async with self._cond:
            while not self._buf and not self._finished:
                await self._cond.wait()
            if not self._buf:
                return None
            frame = self._buf.popleft()
            self._bytes -= len(frame)
            self.frames_out += 1
            self.bytes_out += len(frame)
            self._maybe_resume_locked()
            return frame

    async def finish(self) -> None:
        """Signal end-of-stream; pending/future reads drain then return None."""
        async with self._cond:
            self._finished = True
            self._cond.notify_all()

    def _maybe_resume_locked(self) -> None:
        if not self._paused or self._bytes > self.low_watermark:
            return
        if self.min_pause_seconds > 0.0:
            remaining = self.min_pause_seconds - (
                time.monotonic() - self._pause_started
            )
            if remaining > 0.0:
                if not self._resume_scheduled:
                    self._resume_scheduled = True
                    asyncio.get_running_loop().call_later(
                        remaining, self._resume_timer
                    )
                return
        self._paused = False
        self.resume_count += 1
        self._cond.notify_all()  # wake the paused producer

    def _resume_timer(self) -> None:
        self._resume_scheduled = False
        asyncio.ensure_future(self._resume_after_timer())

    async def _resume_after_timer(self) -> None:
        async with self._cond:
            self._maybe_resume_locked()

    def __aiter__(self) -> "BackpressureStream":
        return self

    async def __anext__(self) -> bytes:
        frame = await self.read()
        if frame is None:
            raise StopAsyncIteration
        return frame

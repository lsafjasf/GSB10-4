"""Streaming backpressure with high/low water marks (stdlib only, asyncio).

Water-mark rules
----------------
The stream buffers whole frames (bytes objects) in a FIFO. Buffer occupancy
is measured in *bytes*.

- PAUSE  : after a write, if buffered_bytes >= high_water, the stream enters
           the paused state. Producers block in write() until resumed.
- RESUME : after a read, if the stream is paused AND
           buffered_bytes <= low_water, the stream leaves the paused state
           and blocked producers are woken.

The gap (low_water < buffered_bytes < high_water) is a hysteresis band:
while inside it the stream keeps its current state, so a consumer hovering
near a single threshold cannot cause pause/resume flapping.

Memory upper bound (provable)
-----------------------------
A producer checks the paused flag *before* enqueueing a frame, and the flag
is set synchronously (no await) right after the frame that crosses
high_water. Therefore at most one frame can be enqueued while
buffered_bytes < high_water, giving:

    buffered_bytes < high_water + max_frame_size

i.e. peak buffer memory is bounded by high_water + max_frame_size,
independent of the total number of frames. A single frame larger than
high_water still passes through (it is never split or dropped), and the
bound degrades gracefully to that frame's own size.

No loss / no duplication
------------------------
Frames move through exactly one deque with exactly one popleft() per
append(); close() only takes effect after the buffer drains, so every
written frame is delivered exactly once, in FIFO order.
"""

import asyncio
import collections

__all__ = ["BackpressureStream"]


class BackpressureStream:
    """A single-producer/single-consumer byte-frame stream with backpressure."""

    def __init__(self, high_water=65536, low_water=16384):
        if not (0 <= low_water <= high_water):
            raise ValueError("need 0 <= low_water <= high_water")
        self.high_water = high_water
        self.low_water = low_water
        self._buf = collections.deque()
        self._bytes = 0
        self._paused = False
        self._closed = False
        self._resumed = asyncio.Event()
        self._resumed.set()
        self._has_data = asyncio.Event()
        # observability counters
        self.pause_count = 0
        self.resume_count = 0
        self.frames_written = 0
        self.frames_read = 0
        self.max_buffered_bytes = 0

    @property
    def buffered_bytes(self):
        return self._bytes

    @property
    def paused(self):
        return self._paused

    async def write(self, frame):
        """Enqueue one frame; blocks while the stream is paused."""
        if self._closed:
            raise RuntimeError("write on closed stream")
        if not isinstance(frame, (bytes, bytearray, memoryview)):
            raise TypeError("frame must be bytes-like")
        # Block (without holding any state) while paused.
        while self._paused:
            await self._resumed.wait()
        frame = bytes(frame)
        self._buf.append(frame)
        self._bytes += len(frame)
        self.frames_written += 1
        if self._bytes > self.max_buffered_bytes:
            self.max_buffered_bytes = self._bytes
        self._has_data.set()
        # Pause check happens synchronously after the enqueue: no other
        # coroutine can interleave between the append and this check.
        if not self._paused and self._bytes >= self.high_water:
            self._paused = True
            self._resumed.clear()
            self.pause_count += 1

    async def read(self):
        """Dequeue one frame; returns None at end-of-stream."""
        while True:
            if self._buf:
                frame = self._buf.popleft()
                self._bytes -= len(frame)
                self.frames_read += 1
                if not self._buf:
                    self._has_data.clear()
                if self._paused and self._bytes <= self.low_water:
                    self._paused = False
                    self._resumed.set()
                    self.resume_count += 1
                return frame
            if self._closed:
                return None
            await self._has_data.wait()

    async def close(self):
        """Signal end-of-stream. Takes effect after the buffer drains."""
        self._closed = True
        self._has_data.set()  # wake a waiting reader so it can see EOF
        self._resumed.set()   # never leave a producer stuck (defensive)

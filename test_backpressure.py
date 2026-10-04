"""Self-tests for BackpressureStream (stdlib unittest only)."""

import asyncio
import hashlib
import random
import time
import tracemalloc
import unittest

from backpressure import BackpressureStream


def make_frame(i: int, size: int) -> bytes:
    """Deterministic frame: cheap to regenerate, no need to preallocate."""
    rep = (size + 7) // 8
    return (i.to_bytes(8, "big") * rep)[:size]


async def pump(stream, frames, producer_yield_every=None):
    for i, f in enumerate(frames):
        await stream.write(f)
        if producer_yield_every is not None and i and i % producer_yield_every == 0:
            await asyncio.sleep(0)
    await stream.finish()


class BackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def _drain_all(self, stream, per_frame_sleep=0.0, sleep_every=None,
                         count=0, extend_into=None):
        received = []
        n = 0
        async for frame in stream:
            received.append(frame)
            if extend_into is not None:
                extend_into(frame)
            n += 1
            if per_frame_sleep and n % sleep_every == 0:
                await asyncio.sleep(per_frame_sleep)
        if count:
            self.assertEqual(n, count)
        return received

    # 1. Extremely slow consumer: no frame loss / duplication, bound respected.
    async def test_slow_consumer_no_loss_no_dup(self):
        high, low, fsize, n = 4096, 1024, 64, 3000
        stream = BackpressureStream(high, low)
        frames = [make_frame(i, fsize) for i in range(n)]

        async def consumer():
            out = []
            async for frame in stream:
                out.append(frame)
                await asyncio.sleep(0.0002)  # 0.6 s of slow consumption
            return out

        out = (await asyncio.gather(pump(stream, frames), consumer()))[1]
        self.assertEqual(out, frames)
        self.assertEqual(stream.frames_out, n)
        self.assertGreater(stream.pause_count, 0)
        self.assertEqual(stream.pause_count, stream.resume_count)
        self.assertFalse(stream.paused)
        self.assertLess(stream.max_buffered_bytes, high + fsize)

    # 2. Chatter suppression: hysteresis gap + min dwell cut flip frequency.
    async def _jitter_run(self, high, low, min_pause=0.0):
        n, fsize = 4000, 64
        stream = BackpressureStream(high, low, min_pause_seconds=min_pause)
        got = 0

        async def producer():
            for i in range(n):
                await stream.write(make_frame(i, fsize))
            await stream.finish()

        async def consumer():
            nonlocal got
            # Bursty speed: fast then a stall, oscillating around the watermark.
            async for _ in stream:
                got += 1
                if got % 7 == 0:
                    await asyncio.sleep(0.001)

        await asyncio.gather(producer(), consumer())
        self.assertEqual(got, n)  # nothing lost
        return stream

    async def test_jitter_suppression(self):
        narrow = await self._jitter_run(high=8192, low=8064)          # 2-frame gap
        wide = await self._jitter_run(high=8192, low=2048)            # 96-frame gap
        dwell = await self._jitter_run(high=8192, low=2048,
                                       min_pause=0.01)                # 10 ms dwell
        print(f"\n  jitter pause/resume counts: narrow={narrow.pause_count}, "
              f"wide={wide.pause_count}, wide+dwell={dwell.pause_count}")
        self.assertLess(wide.pause_count, narrow.pause_count)
        self.assertLessEqual(dwell.pause_count, wide.pause_count)
        self.assertEqual(wide.pause_count, wide.resume_count)

    # 3. Empty stream: immediate EOF, zero pauses.
    async def test_empty_stream(self):
        stream = BackpressureStream(4096, 1024)
        await stream.finish()
        self.assertIsNone(await stream.read())
        self.assertEqual(stream.pause_count, 0)
        self.assertEqual(stream.max_buffered_bytes, 0)

    # 4. One frame far larger than the high watermark: delivered whole.
    async def test_single_oversized_message(self):
        high, low = 1024, 256
        big = bytes(range(256)) * 40  # 10240 bytes = 10 * high
        stream = BackpressureStream(high, low)
        frames = [big] + [make_frame(i, 60) for i in range(100)]

        async def consumer():
            out = []
            async for frame in stream:
                out.append(frame)
            return out

        out = (await asyncio.gather(pump(stream, frames), consumer()))[1]
        self.assertEqual(out, frames)
        self.assertEqual(len(out[0]), 10240)
        self.assertEqual(stream.max_buffered_bytes, len(big))
        self.assertEqual(stream.pause_count, stream.resume_count)
        self.assertGreaterEqual(stream.pause_count, 1)

    # 5. Byte-for-byte identical to an entirely unlimited pipeline ("duipai").
    async def test_byte_identical_to_unthrottled(self):
        rng = random.Random(20261005)
        n = 5000
        frames = [rng.randbytes(rng.randint(1, 300)) for _ in range(n)]
        reference = b"".join(frames)
        ref_hash = hashlib.sha256(reference).hexdigest()
        print(f"\n  reference (unthrottled) sha256={ref_hash}")
        for high, low, min_pause in (
            (1024, 512, 0.0),
            (65536, 4096, 0.0),
            (2048, 1, 0.001),
        ):
            stream = BackpressureStream(high, low, min_pause_seconds=min_pause)
            sink = bytearray()
            jitter_rng = random.Random(1)

            async def consumer():
                async for frame in stream:
                    sink.extend(frame)
                    if jitter_rng.random() < 0.05:
                        await asyncio.sleep(0)

            await asyncio.gather(pump(stream, frames), consumer())
            got_hash = hashlib.sha256(sink).hexdigest()
            print(f"  backpressured H={high:6d} L={low:5d} "
                  f"dwell={min_pause:g} pauses={stream.pause_count:4d} "
                  f"sha256={got_hash}")
            self.assertEqual(bytes(sink), reference)
            self.assertEqual(got_hash, ref_hash)

    # 6. Memory bound: independent of total message count.
    async def _memory_run(self, n, high=32768, low=8192, fsize=128):
        stream = BackpressureStream(high, low)

        async def producer():
            for i in range(n):
                await stream.write(make_frame(i, fsize))
                if i and i % 1000 == 0:
                    await asyncio.sleep(0)
            await stream.finish()

        async def consumer():
            async for _ in stream:
                pass

        tracemalloc.start()
        t0 = time.monotonic()
        await asyncio.gather(producer(), consumer())
        elapsed = time.monotonic() - t0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return peak, stream.max_buffered_bytes, stream.pause_count, elapsed

    async def test_memory_bound_independent_of_count(self):
        print()
        peaks = {}
        for n in (20_000, 80_000, 160_000):
            peak, maxbuf, pauses, elapsed = await self._memory_run(n)
            peaks[n] = peak
            print(f"  messages={n:7d}  peak_python_alloc={peak/1024:8.1f} KiB  "
                  f"max_buffered={maxbuf} B  pauses={pauses:4d}  "
                  f"time={elapsed:.3f}s")
            # Deterministic per-stream bound from the invariant.
            self.assertLess(maxbuf, 32768 + 128)
        # 160k messages must not need materially more Python heap than 20k.
        self.assertLess(peaks[160_000], peaks[20_000] * 2 + 1_000_000)

    async def test_bad_configuration_rejected(self):
        with self.assertRaises(ValueError):
            BackpressureStream(high_watermark=1024, low_watermark=1024)
        with self.assertRaises(ValueError):
            BackpressureStream(high_watermark=1024, low_watermark=4096)
        with self.assertRaises(ValueError):
            BackpressureStream(high_watermark=1024, min_pause_seconds=-1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

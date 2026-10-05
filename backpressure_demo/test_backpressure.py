"""Self tests for BackpressureStream (python3 -m unittest -v)."""

import asyncio
import gc
import os
import time
import unittest

from backpressure import BackpressureStream


def _rss_kb():
    """Resident set size in KiB (Linux), or 0 when unavailable."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def _make_messages(n, size=4096, seed=1234):
    msgs = []
    x = seed
    for i in range(n):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        payload = f"{i}:".encode()
        msgs.append(payload + bytes([x & 0xFF]) * (size - len(payload)))
    return msgs


async def _run_bp(messages, high, low, consumer_ticks=1, producer_ticks=None,
                  delays=None, track_peak=False):
    """Run messages through a backpressure stream.

    consumer_ticks: consumer calls sleep(0) every N frames (1 = slowest).
    delays: optional list/function giving per-frame consumer sleep seconds,
            used to synthesize jitter.
    """
    stream = BackpressureStream(high, low)
    out = bytearray()
    producer_ticks = producer_ticks if producer_ticks is not None else max(1, consumer_ticks * 8)

    async def producer():
        for i, m in enumerate(messages):
            await stream.write(m)
            if i % producer_ticks == 0:
                await asyncio.sleep(0)
        await stream.close()

    async def consumer():
        i = 0
        while True:
            frame = await stream.read()
            if frame is None:
                break
            out.extend(frame)
            if delays is not None:
                d = delays[i] if isinstance(delays, list) else delays(i)
                if d:
                    await asyncio.sleep(d)
                elif i % consumer_ticks == 0:
                    await asyncio.sleep(0)
            elif i % consumer_ticks == 0:
                await asyncio.sleep(0)
            i += 1

    await asyncio.gather(producer(), consumer())
    return bytes(out), stream


async def _run_unlimited(messages, delays=None, consumer_ticks=1):
    """Reference pipeline: unbounded asyncio.Queue (no backpressure at all)."""
    q = asyncio.Queue()
    SENTINEL = object()
    out = bytearray()

    async def producer():
        for m in messages:
            await q.put(m)
            await asyncio.sleep(0)
        await q.put(SENTINEL)

    async def consumer():
        i = 0
        while True:
            frame = await q.get()
            if frame is SENTINEL:
                break
            out.extend(frame)
            if delays is not None:
                d = delays[i] if isinstance(delays, list) else delays(i)
                if d:
                    await asyncio.sleep(d)
                elif i % consumer_ticks == 0:
                    await asyncio.sleep(0)
            elif i % consumer_ticks == 0:
                await asyncio.sleep(0)
            i += 1

    await asyncio.gather(producer(), consumer())
    return bytes(out)


class TestBackpressure(unittest.TestCase):

    def test_slow_consumer_exact_once_and_bound(self):
        """Very slow consumer: every frame delivered once, order kept, bound holds."""
        msgs = _make_messages(300, size=1024)
        high, low = 8192, 2048  # 8 frames / 2 frames
        out, s = asyncio.run(_run_bp(msgs, high, low, consumer_ticks=1))
        self.assertEqual(out, b"".join(msgs))
        self.assertEqual(s.frames_written, 300)
        self.assertEqual(s.frames_read, 300)
        self.assertGreater(s.pause_count, 0, "slow consumer must trigger pauses")
        self.assertEqual(s.pause_count, s.resume_count,
                         "every pause is matched by a resume before EOF")
        max_frame = max(len(m) for m in msgs)
        self.assertLessEqual(s.max_buffered_bytes, high + max_frame)
        self.assertLess(s.max_buffered_bytes, high + max_frame + 1)

    def test_empty_stream(self):
        """Empty flow: EOF immediately, no frames, no pauses, no errors."""
        out, s = asyncio.run(_run_bp([], 1024, 256))
        self.assertEqual(out, b"")
        self.assertEqual((s.frames_written, s.frames_read), (0, 0))
        self.assertEqual((s.pause_count, s.resume_count), (0, 0))

    def test_single_oversized_message(self):
        """One frame far larger than high_water still passes intact."""
        big = bytes((i * 7) & 0xFF for i in range(8 * 1024 * 1024))  # 8 MiB
        high, low = 64 * 1024, 16 * 1024
        out, s = asyncio.run(_run_bp([big], high, low, consumer_ticks=1))
        self.assertEqual(out, big)
        self.assertEqual(s.frames_read, 1)
        self.assertEqual(s.pause_count, 1)
        self.assertEqual(s.resume_count, 1)
        # Bound: high_water + single frame size.
        self.assertLessEqual(s.max_buffered_bytes, high + len(big))

    def test_byte_identical_to_unlimited_pipeline(self):
        """Dui-pai: backpressure output is byte-identical to no-limit output."""
        msgs = _make_messages(500, size=2048)
        high, low = 16384, 4096
        out_bp, s = asyncio.run(_run_bp(msgs, high, low, consumer_ticks=2))
        out_ref = asyncio.run(_run_unlimited(msgs, consumer_ticks=2))
        self.assertEqual(len(out_bp), len(b"".join(msgs)))
        self.assertEqual(out_bp, out_ref, "backpressure vs unlimited mismatch")
        self.assertEqual(out_bp, b"".join(msgs))
        self.assertGreater(s.pause_count, 0)
        print(f"\n[dui-pai] frames=500 bytes={len(out_bp)} "
              f"bp==unlimited==source  pauses={s.pause_count}")

    def test_jitter_hysteresis_suppresses_flapping(self):
        """Jittery consumer: compare hysteresis (low<high) vs no band (low==high)."""
        n = 400
        msgs = _make_messages(n, size=2048)
        high = 16384

        def delays(i):
            phase = (i // 40) % 2
            if phase == 0:
                return 0.002  # slow burst
            return 0.0        # fast burst

        out_h, sh = asyncio.run(
            _run_bp(msgs, high, 4096, delays=delays, consumer_ticks=1))
        out_n, sn = asyncio.run(
            _run_bp(msgs, high, high, delays=delays, consumer_ticks=1))
        self.assertEqual(out_h, b"".join(msgs))
        self.assertEqual(out_n, b"".join(msgs))
        print(f"\n[jitter] no band (low==high): pauses={sn.pause_count} "
              f"resumes={sn.resume_count}")
        print(f"[jitter] hysteresis 12 KiB : pauses={sh.pause_count} "
              f"resumes={sh.resume_count}")
        self.assertLess(sh.pause_count, sn.pause_count,
                        "hysteresis must reduce pause/resume flips")
        self.assertEqual(sh.pause_count, sh.resume_count)

    def test_memory_does_not_grow_with_message_count(self):
        """Long run: buffer occupancy and RSS stay flat as frame count grows."""
        high, low = 65536, 16384
        size = 4096
        n = 20_000  # 80 MiB of payload streamed
        stream = BackpressureStream(high, low)
        samples = []  # (frames_read, buffered_bytes, rss_kb)
        rss = [0]

        async def producer():
            x = 7
            for i in range(n):
                x = (x * 1103515245 + 12345) & 0x7FFFFFFF
                await stream.write(f"{i}:".encode() + bytes([x & 0xFF]) * (size - 2))
                if i % 32 == 0:
                    await asyncio.sleep(0)
            await stream.close()

        async def consumer():
            i = 0
            while True:
                frame = await stream.read()
                if frame is None:
                    break
                if i % 2 == 0:
                    await asyncio.sleep(0)  # slower than producer -> pressure
                if i % 2000 == 0:
                    if i and i % 4000 == 0:
                        gc.collect()
                    samples.append((i, stream.buffered_bytes, _rss_kb()))
                i += 1

        async def run_all():
            await asyncio.gather(producer(), consumer())

        asyncio.run(run_all())

        # 1) queue-level bound, independent of n
        self.assertLessEqual(stream.max_buffered_bytes, high + size)
        self.assertEqual(stream.frames_read, n)

        # 2) process RSS: after warmup it must stay flat (well below the
        #    80 MiB total payload; a few MiB of interpreter noise allowed).
        warm = samples[3:]
        rss_values = [r for _, _, r in warm if r]
        print("\n[memory] frames_read  buffered_B  RSS_KiB")
        for i, b, r in samples:
            print(f"[memory] {i:>11} {b:>10} {r:>9}")
        growth = max(rss_values) - min(rss_values)
        total = n * size
        self.assertLess(growth, 8 * 1024,
                        f"RSS grew {growth} KiB while {total // 1024} KiB streamed")
        self.assertLess(max(rss_values), total // 1024 // 2,
                        "RSS must be a small fraction of total streamed bytes")

    def test_watermark_validation(self):
        with self.assertRaises(ValueError):
            BackpressureStream(high_water=100, low_water=200)
        with self.assertRaises(ValueError):
            BackpressureStream(high_water=-1, low_water=0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

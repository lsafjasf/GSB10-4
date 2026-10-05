"""Benchmark: 1,000,000 frames through BackpressureStream.

Reports total wall time, pause/resume counts, peak buffered bytes and
periodic RSS samples to show memory does not grow with message count.
"""

import asyncio
import gc
import time

from backpressure import BackpressureStream

N = 1_000_000
FRAME_SIZE = 128          # 128 MiB of payload in total
HIGH = 1 << 20            # 1 MiB
LOW = 256 << 10           # 256 KiB


def _rss_kb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


async def main():
    stream = BackpressureStream(HIGH, LOW)
    samples = []
    consumed = [0]
    consumed_bytes = [0]

    async def producer():
        x = 42
        for i in range(N):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
            await stream.write(x.to_bytes(4, "little") + bytes(FRAME_SIZE - 4))
            if i % 256 == 0:
                await asyncio.sleep(0)
        await stream.close()

    async def consumer():
        i = 0
        while True:
            frame = await stream.read()
            if frame is None:
                break
            consumed_bytes[0] += len(frame)
            if i % 32 == 0:
                await asyncio.sleep(0)  # consumer ~8x slower than producer
            if i % 100_000 == 0:
                if i:
                    gc.collect()
                samples.append((i, stream.buffered_bytes, _rss_kb()))
            i += 1
        consumed[0] = i

    t0 = time.perf_counter()
    await asyncio.gather(producer(), consumer())
    dt = time.perf_counter() - t0

    print(f"frames            : {consumed[0]:,} / {N:,}")
    print(f"payload           : {consumed_bytes[0] / 2**20:.1f} MiB")
    print(f"total time        : {dt:.3f} s  ({N / dt:,.0f} frames/s)")
    print(f"pauses            : {stream.pause_count}")
    print(f"resumes           : {stream.resume_count}")
    print(f"peak buffered     : {stream.max_buffered_bytes:,} B "
          f"(bound = high_water + frame = {HIGH + FRAME_SIZE:,} B)")
    print(f"bound holds       : {stream.max_buffered_bytes <= HIGH + FRAME_SIZE}")
    print("frames_read   buffered_B   RSS_KiB")
    for i, b, r in samples:
        print(f"{i:>11,} {b:>12,} {r:>10,}")
    warm = [r for _, _, r in samples[2:]]
    print(f"RSS drift after warmup: {max(warm) - min(warm):,} KiB "
          f"over {len(warm)} samples / {N:,} frames")
    assert consumed[0] == N
    assert consumed_bytes[0] == N * FRAME_SIZE
    assert stream.max_buffered_bytes <= HIGH + FRAME_SIZE


if __name__ == "__main__":
    asyncio.run(main())

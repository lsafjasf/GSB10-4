"""Benchmarks / data for BackpressureStream (stdlib only).

Sections:
  A. 1,000,000 messages: total time, pause/resume counts, byte-identical hash.
  B. Long-run RSS samples every 100k messages: proves memory does not grow
     with the total message count.
  C. Jitter input: pause counts for narrow gap vs wide hysteresis vs dwell.
"""

import asyncio
import hashlib
import time

from backpressure import BackpressureStream


def make_frame(i: int, size: int) -> bytes:
    rep = (size + 7) // 8
    return (i.to_bytes(8, "big") * rep)[:size]


def rss_kb() -> int:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        return -1
    return -1


async def bench_one_million():
    n, fsize = 1_000_000, 100
    high, low = 1 << 20, 1 << 18  # 1 MiB / 256 KiB
    stream = BackpressureStream(high, low)
    src_hash = hashlib.sha256()
    out_hash = hashlib.sha256()
    rss_samples = []

    async def producer():
        for i in range(n):
            frame = make_frame(i, fsize)
            src_hash.update(frame)
            await stream.write(frame)
            if i % 100_000 == 0:
                rss_samples.append((i, rss_kb(), stream.max_buffered_bytes))
        await stream.finish()

    async def consumer():
        read = stream.read
        while True:
            frame = await read()
            if frame is None:
                break
            out_hash.update(frame)
            # Consumer is deliberately a bit slower so backpressure engages.
            if stream.frames_out % 64 == 0:
                await asyncio.sleep(0)

    t0 = time.monotonic()
    await asyncio.gather(producer(), consumer())
    elapsed = time.monotonic() - t0
    rss_samples.append((n, rss_kb(), stream.max_buffered_bytes))

    print("=== A. 1,000,000 messages (100 B each, H=1 MiB, L=256 KiB) ===")
    print(f"  total time          : {elapsed:.3f} s")
    print(f"  throughput          : {n / elapsed:,.0f} msg/s "
          f"({n * fsize / elapsed / 1e6:.1f} MiB/s)")
    print(f"  pause count         : {stream.pause_count}")
    print(f"  resume count        : {stream.resume_count}")
    print(f"  max buffered bytes  : {stream.max_buffered_bytes} "
          f"(bound H+frame = {high + fsize})")
    print(f"  frames in / out     : {stream.frames_in} / {stream.frames_out}")
    print(f"  source sha256       : {src_hash.hexdigest()}")
    print(f"  consumer sha256     : {out_hash.hexdigest()}")
    print(f"  byte-identical      : {src_hash.digest() == out_hash.digest()}")

    print("\n=== B. RSS during the run (sampled every 100k messages) ===")
    print("  messages       RSS(KiB)  max_buffered(B)")
    for i, rss, maxbuf in rss_samples:
        print(f"  {i:>9,}  {rss:>9,}  {maxbuf:>15,}")
    rss_vals = [r for _, r, _ in rss_samples if r > 0]
    if rss_vals:
        growth = rss_vals[-1] - min(rss_vals)
        print(f"  RSS growth after first sample: {growth} KiB "
              f"(total data streamed: {n * fsize / 1e6:.0f} MiB)")


async def jitter_demo():
    print("\n=== C. Jitter input: pause counts (4000 frames, bursty consumer) ===")

    async def run(high, low, min_pause=0.0):
        n, fsize = 4000, 64
        stream = BackpressureStream(high, low, min_pause_seconds=min_pause)
        got = 0

        async def producer():
            for i in range(n):
                await stream.write(make_frame(i, fsize))
            await stream.finish()

        async def consumer():
            nonlocal got
            async for _ in stream:
                got += 1
                if got % 7 == 0:
                    await asyncio.sleep(0.001)

        await asyncio.gather(producer(), consumer())
        assert got == n
        return stream

    narrow = await run(8192, 8064)
    wide = await run(8192, 2048)
    dwell = await run(8192, 2048, min_pause=0.01)
    print(f"  narrow gap (2 frames)        : {narrow.pause_count:4d} pauses / "
          f"{narrow.resume_count} resumes")
    print(f"  wide hysteresis (96 frames)  : {wide.pause_count:4d} pauses / "
          f"{wide.resume_count} resumes")
    print(f"  wide hysteresis + 10ms dwell : {dwell.pause_count:4d} pauses / "
          f"{dwell.resume_count} resumes")


async def main():
    await bench_one_million()
    await jitter_demo()


if __name__ == "__main__":
    asyncio.run(main())

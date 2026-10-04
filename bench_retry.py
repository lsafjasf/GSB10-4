"""Benchmark: 1,000,000 retries on an injected virtual clock.

Run: python3 bench_retry.py
"""

import asyncio
import random
import resource
import time
import tracemalloc

from retrylib import BackoffPolicy, Status, retry_call

TOTAL_RETRIES = 1_000_000
RETRIES_PER_OP = 10  # max_attempts=11 -> 10 sleeps per op, always exhausted


class BenchClock:
    """Non-yielding virtual clock: records time, never blocks."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = 0

    async def sleep(self, delay):
        self.now += delay
        self.sleeps += 1


async def main():
    policy = BackoffPolicy(max_attempts=RETRIES_PER_OP + 1)
    rng = random.Random(42)
    clock = BenchClock()

    async def always_fail():
        raise RuntimeError("boom")

    ops = TOTAL_RETRIES // RETRIES_PER_OP
    tracemalloc.start()
    t0 = time.perf_counter()
    exhausted = 0
    for _ in range(ops):
        res = await retry_call(always_fail, policy, rng, clock.sleep)
        if res.status is Status.EXHAUSTED:
            exhausted += 1
    wall = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    retries = clock.sleeps
    print(f"retries performed   : {retries:,} (across {ops:,} ops, all EXHAUSTED={exhausted == ops})")
    print(f"wall time           : {wall:.3f} s")
    print(f"throughput          : {retries / wall:,.0f} retries/s")
    print(f"avg per retry       : {wall / retries * 1e9:,.0f} ns")
    print(f"tracemalloc peak    : {peak / 1024 / 1024:.3f} MiB")
    print(f"process RSS peak    : {rss_mib:.1f} MiB")
    print(f"virtual time slept  : {clock.now:,.1f} s (injected clock, no real waiting)")


if __name__ == "__main__":
    asyncio.run(main())

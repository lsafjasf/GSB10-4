"""Jitter distribution data + 1M-retry benchmark.
Run: python3 bench_retry.py
"""
import asyncio
import gc
import random
import resource
import time
import tracemalloc

from async_retry import RetryExecutor, Outcome, SOURCE_EXECUTED

BASE, CAP = 0.1, 5.0
SEED = 42
N_SAMPLES = 100_000
N_RETRIES = 1_000_000
BINS = 10


def jitter_data():
    print("=" * 70)
    print("1) FULL JITTER DISTRIBUTION")
    print("   formula: delay = rand() * min(cap, base * 2**attempt)")
    print(f"   base={BASE}s, cap={CAP}s, rand~U[0,1), samples={N_SAMPLES}, "
          f"seed={SEED}")
    print()
    for attempt in (0, 1, 2, 6):
        limit = min(CAP, BASE * 2 ** attempt)
        rnd = random.Random(SEED)
        samples = [rnd.random() * limit for _ in range(N_SAMPLES)]
        mean = sum(samples) / len(samples)
        histo = [0] * BINS
        for s in samples:
            idx = min(BINS - 1, int(s / limit * BINS))
            histo[idx] += 1
        print(f"attempt={attempt}: limit=min({CAP}, {BASE*2**attempt:g}) "
              f"= {limit:g}s   bounds=[0, {limit:g})   "
              f"E[delay]={limit/2:g}s   observed mean={mean:.6f}s")
        print(f"  {'range (s)':>22} | {'count':>7} | bar")
        for i, c in enumerate(histo):
            lo, hi = i * limit / BINS, (i + 1) * limit / BINS
            bar = "#" * round(c / N_SAMPLES * 50)
            edge = ")"
            print(f"  [{lo:>9.4f}, {hi:>9.4f}{edge} | {c:>7d} | {bar}")
        print()


def reproducible_sequence():
    print("=" * 70)
    print("2) REPRODUCIBLE JITTER SEQUENCE (seed=42, first 10 retries)")
    def run():
        rnd = random.Random(SEED)
        out, attempt = [], 0
        for _ in range(10):
            limit = min(CAP, BASE * 2 ** attempt)
            out.append(rnd.random() * limit)
            attempt += 1
        return out
    a, b = run(), run()
    for i, (x, y) in enumerate(zip(a, b)):
        limit = min(CAP, BASE * 2 ** i)
        print(f"  retry {i:2d} (limit {limit:>4.1f}s): {x:.17f}  "
              f"run2={y:.17f}  identical={x == y}")
    print()


class InstantClock:
    """Injected time: no real waiting; counts retried sleeps."""
    def __init__(self):
        self.sleeps = 0
        self.simulated_seconds = 0.0
    async def sleep(self, d):
        self.sleeps += 1
        self.simulated_seconds += d


async def benchmark_million():
    print("=" * 70)
    print("3) BENCHMARK: 1,000,000 retries (time injected, zero wall waiting)")
    clock = InstantClock()
    ex = RetryExecutor(max_attempts=2, base_delay=BASE, cap_delay=CAP,
                       sleep=clock.sleep, seed=SEED)
    state = {"n": 0}

    async def fail_once():
        state["n"] += 1
        if state["n"] % 2:
            raise RuntimeError("transient")
        return "ok"

    gc.collect()
    tracemalloc.start()
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.perf_counter()

    outcomes = 0
    while clock.sleeps < N_RETRIES:
        out = await ex.execute(f"key-{state['n']}", fail_once)
        outcomes += 1
        assert isinstance(out, Outcome) and out.source == SOURCE_EXECUTED

    wall = time.perf_counter() - t0
    _, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f"  retry sleeps (jitter computed+awaited): {clock.sleeps:,}")
    print(f"  execute() calls: {outcomes:,}  fn invocations: {state['n']:,}")
    print(f"  wall time: {wall:.3f} s "
          f"({N_RETRIES/wall:,.0f} retries/s)")
    print(f"  Python object peak (tracemalloc): {py_peak/1024/1024:.2f} MiB")
    print(f"  process RSS peak (ru_maxrss): {rss_after/1024:.1f} MiB "
          f"(before run {rss_before/1024:.1f} MiB)")
    print(f"  simulated jitter time that real wall would have spent: "
          f"{clock.simulated_seconds:,.1f} s")
    print()


def dedup_demo():
    print("=" * 70)
    print("4) DEDUP STATISTICS (1000 concurrent callers, 1 key)")

    async def main():
        clock = InstantClock()
        ex = RetryExecutor(max_attempts=3, sleep=clock.sleep, seed=SEED)
        gate = asyncio.Event()
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            await gate.wait()
            return "shared"

        callers = [asyncio.ensure_future(ex.execute("hot", fn))
                   for _ in range(1000)]
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(*callers)
        executed = sum(r.source == "executed" for r in results)
        merged = sum(r.source == "deduplicated" for r in results)
        print(f"  submitted: {ex.stats.submitted}")
        print(f"  fn actually executed: {calls}")
        print(f"  executed outcomes: {executed}")
        print(f"  merged (duplicate requests folded): {merged}  "
              f"== stats.merged={ex.stats.merged}")
        print(f"  saved fn executions: {ex.stats.merged} / 1000 "
              f"({ex.stats.merged/10:.0f}% of traffic)")

    asyncio.run(main())
    print()


if __name__ == "__main__":
    jitter_data()
    reproducible_sequence()
    asyncio.run(benchmark_million())
    dedup_demo()

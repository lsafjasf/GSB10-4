"""Benchmarks for sliding_median.

Part 1: linearity check -- fixed k, growing n, expect time ~ n.
Part 2: headline run -- n=1,000,000, k=1000, wall time + peak memory.

Run: python3 benchmark.py
"""

import random
import resource
import time
import tracemalloc

from sliding_median import sliding_window_medians
from self_test import naive_medians


def checksum(medians):
    """Consume medians without storing them; keeps results observable.

    Mixes numerator/denominator instead of hash(): hashing Fractions goes
    through functools.lru_cache inside fractions._hash_algorithm, which
    would pollute the memory measurement with a 2**14-entry cache.
    """
    acc = 0
    for m in medians:
        num = getattr(m, "numerator", m)
        den = getattr(m, "denominator", 1)
        acc = ((acc * 1000003) ^ num ^ (den << 32)) & 0xFFFFFFFFFFFFFFFF
    return acc


def run_once(values, k):
    t0 = time.perf_counter()
    digest = checksum(sliding_window_medians(values, k))
    return time.perf_counter() - t0, digest


def peak_rss_mib():
    # Linux: ru_maxrss is in KiB.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main():
    rng = random.Random(12345)

    print("== Linearity: k=1000 fixed, n doubles ==")
    print(f"{'n':>10} {'time(s)':>10} {'us/elem':>10} {'ratio vs prev':>14}")
    k = 1000
    prev = None
    for n in (50_000, 100_000, 200_000, 400_000, 800_000):
        values = [rng.randint(-10**6, 10**6) for _ in range(n)]
        best = min(run_once(values, k)[0] for _ in range(2))
        ratio = f"{best / prev:.2f}x" if prev else "-"
        print(f"{n:>10} {best:>10.3f} {best / n * 1e6:>10.3f} {ratio:>14}")
        prev = best

    print("\n== Headline: n=1,000,000  k=1000 ==")
    n, k = 1_000_000, 1000
    values = [rng.randint(-10**9, 10**9) for _ in range(n)]

    # Correctness spot-check on the prefix against the naive implementation.
    head = 2005
    assert list(sliding_window_medians(values[:head], k)) == naive_medians(values[:head], k)
    print("[ok] prefix of big input matches naive implementation")

    t0 = time.perf_counter()
    digest = checksum(sliding_window_medians(values, k))
    dt = time.perf_counter() - t0
    print(f"wall time (timed run, no tracemalloc): {dt:.3f}s  "
          f"({dt / n * 1e6:.2f} us/elem, checksum={digest})")
    print(f"peak RSS of this process so far:      {peak_rss_mib():.1f} MiB "
          f"(includes the 1M-element input list)")

    # Separate pass for Python-level allocation peak (tracemalloc adds
    # overhead, so it is not part of the timed run above).
    tracemalloc.start()
    checksum(sliding_window_medians(values, k))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"tracemalloc Python alloc peak:        {peak / 2**20:.1f} MiB "
          f"(algorithm structures only; input list allocated before start)")

    print(f"final peak RSS:                       {peak_rss_mib():.1f} MiB")


if __name__ == "__main__":
    main()

"""Print jitter distribution data (histogram, mean, bounds) with a fixed seed.

Run: python3 histogram.py
"""

import random

from retrylib import BackoffPolicy, JitterStrategy

N = 100_000
SEED = 42
BINS = 10


def histogram(data, lo, hi, bins):
    width = (hi - lo) / bins
    counts = [0] * bins
    for x in data:
        idx = min(int((x - lo) / width), bins - 1)
        counts[idx] += 1
    return counts, width


def report(name, policy, attempt):
    rng = random.Random(SEED)
    data = [policy.delay(attempt, rng) for _ in range(N)]
    c = policy.ceiling(attempt)
    mean = sum(data) / len(data)
    print(f"\n== {name} (attempt={attempt}, ceiling c={c}) ==")
    print(f"samples      : {N} (seed={SEED})")
    print(f"min / max    : {min(data):.6f} / {max(data):.6f}")
    if policy.strategy is JitterStrategy.FULL:
        print(f"expected mean: c/2  = {c / 2:.6f}   actual mean: {mean:.6f}")
        lo, hi = 0.0, c
    else:
        print(f"expected mean: 3c/4 = {3 * c / 4:.6f}   actual mean: {mean:.6f}")
        lo, hi = c / 2.0, c
    counts, width = histogram(data, lo, hi, BINS)
    print(f"histogram ({BINS} bins, width={width:.4f}):")
    peak = max(counts)
    for i, cnt in enumerate(counts):
        a = lo + i * width
        bar = "#" * round(40 * cnt / peak)
        print(f"  [{a:7.4f}, {a + width:7.4f})  {cnt:6d}  {bar}")


def main():
    base = BackoffPolicy(base=0.1, cap=10.0)
    report("FULL jitter  U(0, c)", BackoffPolicy(
        base=base.base, cap=base.cap, strategy=JitterStrategy.FULL), attempt=3)
    report("EQUAL jitter c/2 + U(0, c/2)", BackoffPolicy(
        base=base.base, cap=base.cap, strategy=JitterStrategy.EQUAL), attempt=3)

    print("\n== reproducible delay sequence (seed=42, attempts 0..9, FULL) ==")
    rng = random.Random(SEED)
    seq = [base.delay(i, rng) for i in range(10)]
    for i, d in enumerate(seq):
        print(f"  attempt {i}: ceiling={base.ceiling(i):8.4f}  delay={d:.10f}")


if __name__ == "__main__":
    main()

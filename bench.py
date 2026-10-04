"""Benchmark: 1,000,000 hint generations (stdlib only)."""
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ratelimit_hint import (
    FixedWindowLimiter,
    JitterSuggester,
    PerSecondLimiter,
    make_hint,
    validate_hint,
)

N = 1_000_000


def run(name, limiter_factory, steps):
    limiter = limiter_factory()
    jitter = JitterSuggester(seed=42)
    lengths = Counter()
    allowed_count = 0
    start = time.perf_counter()
    for i in range(N):
        now = i * steps  # advance injected time
        allowed = limiter.try_acquire(now_ms=now)
        hint = make_hint(limiter, allowed=allowed, jitterer=jitter, now_ms=now)
        text = hint.format()
        lengths[len(text)] += 1
        allowed_count += allowed
    elapsed = time.perf_counter() - start
    total = sum(lengths.values())
    mean = sum(k * v for k, v in lengths.items()) / total
    print(f"[{name}]")
    print(f"  generations : {N:,}")
    print(f"  elapsed     : {elapsed:.3f}s  ({N / elapsed:,.0f} hints/s, "
          f"{elapsed / N * 1e6:.2f} us/hint)")
    print(f"  allowed     : {allowed_count:,}  denied: {N - allowed_count:,}")
    print(f"  hint length : min={min(lengths)} max={max(lengths)} mean={mean:.1f}")
    print("  length distribution (len: count):")
    for length in sorted(lengths):
        print(f"    {length:>3}: {lengths[length]:>8,} "
              f"({lengths[length] / total * 100:5.1f}%)")
    print()


def main():
    # Per-second: 10 rps bucket of 10, one request every 37ms -> mix of
    # allow/deny, exercises both ALLOW and RETRY formats.
    run("per-second token bucket, 10rps, request every 37ms",
        lambda: PerSecondLimiter(rate_per_sec=10, capacity=10), steps=37)
    # Per-window: 1000 req / 60s window, one request every 37ms -> mostly
    # allowed until the window fills, then RETRY until reset.
    run("per-window fixed window, 1000/60s, request every 37ms",
        lambda: FixedWindowLimiter(limit=1000, window_seconds=60), steps=37)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Benchmark: generate 1,000,000 rate-limited (429) responses and report
wall time + hint/body-length distributions.

Timeline is fully injected: remaining time oscillates over [0, 1s),
including occasional clock rewinds. No wall clock in the hot path.

Run: python3 bench.py [N]
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter

from ratelimit import FixedWindowLimiter, JitterSuggester, ResponseBuilder
from ratelimit.clock import Clock

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
SEED = 0x5EED5EED5EED5EED
SPREAD_MS = 1000


class SweepClock(Clock):
    """Monotonic sweep 0 -> 0.999s in ~1us steps (stays inside window 0 so
    every request is denied), plus a 0.5ms rewind every 100000 steps to
    exercise the clock-skew path."""

    def __init__(self) -> None:
        self.t = 0.0
        self.i = 0
        self.step = 0.999 / N

    def now(self) -> float:
        self.i += 1
        if self.i % 100000 == 0:
            self.t -= 0.0005                    # simulated NTP-style rewind
            return self.t
        self.t += self.step
        return self.t


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def main() -> int:
    clock = SweepClock()
    lim = FixedWindowLimiter(limit=1, window_seconds=1.0, clock=clock, kind="window")
    builder = ResponseBuilder(JitterSuggester(seed=SEED, spread_ms=SPREAD_MS))

    lim.acquire()                               # consume the only quota -> all denied

    hints, bases, jitters, lengths = [], [], [], []
    clamped = 0
    t0 = time.perf_counter()
    for _ in range(N):
        d = lim.acquire()
        r = builder.build(d)
        j = r.jitter
        hints.append(j.hint_ms)
        bases.append(j.base_ms)
        jitters.append(j.jitter_ms)
        lengths.append(len(r.body))
        clamped += int(j.clamped)
    elapsed = time.perf_counter() - t0

    hints.sort(); lengths.sort(); jitters.sort(); bases.sort()

    def stats(v):
        return {
            "min": v[0],
            "p50": round(percentile(v, 50), 2),
            "p90": round(percentile(v, 90), 2),
            "p99": round(percentile(v, 99), 2),
            "p99_9": round(percentile(v, 99.9), 2),
            "max": v[-1],
            "mean": round(sum(v) / len(v), 2),
        }

    # histogram of body byte lengths (16-byte buckets) and of hints (100ms)
    def histogram(v, width):
        c = Counter((x // width) * width for x in v)
        return [{"bucket": int(k), "count": c[k]} for k in sorted(c)]

    report = {
        "n": N,
        "seed": hex(SEED),
        "spread_ms": SPREAD_MS,
        "elapsed_s": round(elapsed, 4),
        "throughput_ops": round(N / elapsed, 1),
        "ns_per_op": round(elapsed / N * 1e9, 1),
        "clamped_count": clamped,
        "clock_skew_count": lim.clock_skew_count,
        "hint_ms": stats(hints),
        "base_ms": stats(bases),
        "jitter_ms": stats(jitters),
        "body_bytes": stats(lengths),
        "body_bytes_histogram": histogram(lengths, 16),
        "hint_ms_histogram": histogram(hints, 100),
    }
    print(json.dumps(report, indent=2))
    with open("reports/bench_report.json", "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nwrote reports/bench_report.json ({N} ops in {elapsed:.3f}s, "
          f"{N / elapsed:,.0f} ops/s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

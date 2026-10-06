"""Benchmarks: 100,000 timeout scenarios + checkpoint scheduling cost.

Two latency notions, reported separately:

1. *Cooperative response latency* (logical, injected virtual time):
   overshoot = (time the interrupt is responded to / cleanup finishes)
               - deadline.  This is the delay forced by running the current
   atomic step to completion.  Deterministic; distribution over 100k tasks.

2. *Scheduling cost* (wall-clock, perf_counter): CPU cost of the runner /
   checkpoint machinery -- ns per timed-out task and ns per checkpoint.

Run:  python3 -m coop_timeout.bench
"""

from __future__ import annotations

import statistics
import time
from typing import List, Tuple

from .coop import (
    TIMEOUT,
    CancellationToken,
    TaskContext,
    VirtualClock,
    run,
)

N = 100_000
N_STEPS = 6


def _lcg(seed: int = 0xC0FFEE) -> int:
    while True:
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        yield seed


def _percentiles(xs: List[int], ps=(50, 90, 95, 99, 99.9, 100)) -> List[Tuple[float, int]]:
    xs = sorted(xs)
    n = len(xs)
    out = []
    for p in ps:
        idx = min(n - 1, (max(1, round(p / 100 * n))) - 1)
        out.append((p, xs[idx]))
    return out


def timeout_batch() -> Tuple[List[int], List[str], int, int]:
    rng = _lcg()
    overshoots: List[int] = []
    stages: List[str] = []
    checkpoints = 0
    releases = 0

    for _ in range(N):
        durs = [8 + (next(rng) % 9) for _ in range(N_STEPS)]  # 8..16 ns each
        deadline = 28 + (next(rng) % 20)  # 28..47: always inside the work body
        clock = VirtualClock()

        def task(ctx: TaskContext, d=durs) -> None:
            def rel():
                nonlocal releases
                releases += 1

            ctx.acquire("RA", rel)
            ctx.acquire("RB", rel)
            for i, d_i in enumerate(d):
                ctx.step("w%d" % i, d_i)

        def probe(ctx, where):
            nonlocal checkpoints
            checkpoints += 1

        r = run(task, clock=clock, deadline=deadline, probe=probe)
        assert r.outcome == TIMEOUT, r.outcome
        overshoots.append(r.overshoot)
        stages.append(r.stage)

    return overshoots, stages, checkpoints, releases


def checkpoint_throughput(iterations: int = 1_000_000) -> float:
    ctx = TaskContext(VirtualClock(), CancellationToken(), deadline=10**18)
    t0 = time.perf_counter_ns()
    for i in range(iterations):
        ctx.checkpoint("c")
    t1 = time.perf_counter_ns()
    return (t1 - t0) / iterations


def main() -> None:
    print("== 100,000 cooperative timeout scenarios (time injected) ==")
    timings = []
    result = None
    for rep in range(3):
        t0 = time.perf_counter_ns()
        result = timeout_batch()
        t1 = time.perf_counter_ns()
        timings.append(t1 - t0)
    overshoots, stages, checkpoints, releases = result

    med = statistics.median(timings)
    print("wall-clock for %d tasks: best=%d ms  median=%d ms  worst=%d ms"
          % (N, min(timings) // 1_000_000, med // 1_000_000, max(timings) // 1_000_000))
    print("runner scheduling cost: median %.3f us/task (%.0f tasks/s)"
          % (med / N / 1000, N / (med / 1e9)))
    print("checkpoints hit: %d total (%.2f/task), resource releases: %d (%.2f/task)"
          % (checkpoints, checkpoints / N, releases, releases / N))
    print()
    print("-- cooperative response latency: overshoot = finished - deadline (virtual ns) --")
    print("min=%d  mean=%.3f  stdev=%.3f"
          % (min(overshoots), statistics.fmean(overshoots), statistics.pstdev(overshoots)))
    for p, v in _percentiles(overshoots):
        print("p%-5s %d" % (p, v))

    # histogram
    width = 4
    buckets = {}
    for v in overshoots:
        buckets[v // width] = buckets.get(v // width, 0) + 1
    print()
    print("overshoot histogram (bucket width %d virtual ns):" % width)
    peak = max(buckets.values())
    for b in sorted(buckets):
        bar = "#" * round(40 * buckets[b] / peak)
        print("  [%2d-%2d) %6d %s" % (b * width, (b + 1) * width, buckets[b], bar))

    from collections import Counter
    print()
    print("responded-at stages:", dict(Counter(stages).most_common()))

    print()
    print("== idle checkpoint scheduling cost (no signal latched) ==")
    ns_cp = checkpoint_throughput()
    print("%.1f ns/checkpoint (%.2f million checkpoints/s)" % (ns_cp, 1000.0 / ns_cp))


if __name__ == "__main__":
    main()

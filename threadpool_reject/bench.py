"""Benchmark: 100_000 submissions against a pool with queue capacity 100.

Reports wall time, throughput, counter conservation, and the rejection-rate
distribution across 10 submission windows of 10_000 each, for every policy.

Rejection rate is measured from the pool's authoritative `rejected` counter
(sampled at each window boundary), so it also captures DISCARD_OLDEST
evictions, which are reported to the evicted task's caller via its Future
rather than through the submit() return value.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from threadpool import Policy, ThreadPool

N = 100_000
CAPACITY = 100
WORKERS = 4
WINDOW = 10_000
TASK_SECONDS = 50e-6  # ~50us of busy work per task


def task():
    deadline = time.perf_counter() + TASK_SECONDS
    while time.perf_counter() < deadline:
        pass


def run(policy: Policy):
    pool = ThreadPool(workers=WORKERS, capacity=CAPACITY, policy=policy)
    rejected_windows = []
    caller_run_windows = []
    prev_rejected = 0
    prev_caller_runs = 0
    t0 = time.perf_counter()
    for i in range(N):
        pool.submit(task)
        if (i + 1) % WINDOW == 0:
            s = pool.stats()
            rejected_windows.append(s.rejected - prev_rejected)
            caller_run_windows.append(s.caller_runs - prev_caller_runs)
            prev_rejected, prev_caller_runs = s.rejected, s.caller_runs
    submit_elapsed = time.perf_counter() - t0
    pool.shutdown(wait=True)
    total_elapsed = time.perf_counter() - t0
    s = pool.stats()
    assert s.conserved, f"conservation violated: {s}"
    assert s.queued == 0 and s.running == 0
    rates = [100.0 * w / WINDOW for w in rejected_windows]
    print(f"policy={policy.value:<15} workers={WORKERS} capacity={CAPACITY} "
          f"task=~{TASK_SECONDS*1e6:.0f}us")
    print(f"  submit loop : {submit_elapsed:8.3f}s  "
          f"({N/submit_elapsed:,.0f} submits/s)")
    print(f"  total (incl. drain): {total_elapsed:8.3f}s")
    print(f"  submitted={s.submitted:,} completed={s.completed:,} "
          f"rejected={s.rejected:,} caller_runs={s.caller_runs:,} "
          f"queued={s.queued} running={s.running}")
    print(f"  conservation: submitted({s.submitted:,}) == completed({s.completed:,})"
          f" + rejected({s.rejected:,}) + running(0) + queued(0)  -> OK")
    print(f"  rejection rate per {WINDOW:,}-submit window (%):")
    print("    " + " ".join(f"{r:6.2f}" for r in rates))
    print(f"    min={min(rates):.2f}%  max={max(rates):.2f}%  "
          f"mean={sum(rates)/len(rates):.2f}%")
    if policy is Policy.CALLER_RUNS:
        print(f"  caller-executed per window: {caller_run_windows}")
    print()
    return s


if __name__ == "__main__":
    for p in (Policy.ABORT, Policy.DISCARD, Policy.DISCARD_OLDEST, Policy.CALLER_RUNS):
        run(p)

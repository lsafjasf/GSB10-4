"""Benchmark: shutdown with 100,000 in-flight tasks.

In-flight = accepted but not finished (queued + running). 64 worker
threads hold running tasks; the rest stay queued. The benchmark reports
the per-phase shutdown timeline, the per-task disposition latency
distribution, and peak memory (tracemalloc + ru_maxrss).
"""

import os
import resource
import statistics
import sys
import threading
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graceful_shutdown import (  # noqa: E402
    Disposition,
    GracefulExecutor,
    Phase,
    Task,
)

N = 100_000
WORKERS = 64


class BenchTask(Task):
    """Never finishes on its own; disposed at shutdown."""

    def __init__(self, task_id, disposition):
        super().__init__(task_id)
        self.disposition = disposition

    def run(self, cancel):
        threading.Event().wait()

    def checkpoint(self):
        return {"id": self.task_id}

    def rollback(self):
        return None


def percentiles(sorted_values, qs=(50, 90, 99, 99.9)):
    out = {}
    n = len(sorted_values)
    for q in qs:
        idx = min(n - 1, max(0, int(q / 100.0 * n)))
        out[q] = sorted_values[idx]
    return out


def main():
    dispositions = [Disposition.CHECKPOINT, Disposition.ROLLBACK,
                    Disposition.FAIL]

    tracemalloc.start()
    t_submit0 = time.monotonic()
    executor = GracefulExecutor(
        max_workers=WORKERS,
        grace_period=0.2,
        dispose_budget_per_task=0.5,
        hard_deadline=30.0,
        poll_interval=0.01,
    )
    for i in range(N):
        executor.submit(BenchTask(f"task-{i}", dispositions[i % 3]))
    submit_wall = time.monotonic() - t_submit0

    time.sleep(0.3)  # let workers pick up tasks
    running = executor.running_count()
    in_flight = executor.in_flight_count()

    t0 = time.monotonic()
    report = executor.shutdown()
    shutdown_wall = time.monotonic() - t0

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    maxrss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    latencies = sorted(
        rec.dispose_latency for rec in report.records.values()
        if rec.dispose_latency is not None
    )
    pcts = percentiles(latencies)

    print(f"in-flight tasks        : {in_flight} "
          f"(running={running}, queued={in_flight - running})")
    print(f"submit wall time       : {submit_wall:.3f}s "
          f"({N / submit_wall:,.0f} tasks/s, not part of shutdown)")
    print()
    print("shutdown timeline (wall clock):")
    for timing in report.phases:
        print(f"  {timing.phase.value:<15} duration={timing.duration:.4f}s")
    print(f"  total (report)     : {report.total_duration:.4f}s")
    print(f"  total (wall)       : {shutdown_wall:.4f}s")
    print(f"  hard deadline      : {report.hard_deadline:.4f} "
          f"(budget {executor.hard_deadline}s) met={report.met_deadline}")
    print()
    print("outcome counts:")
    for outcome, count in sorted(report.outcome_counts().items(),
                                 key=lambda kv: kv[0].value):
        print(f"  {outcome.value:<22} {count}")
    print()
    print("per-task disposition latency (seconds):")
    print(f"  min={latencies[0]:.6f} mean={statistics.fmean(latencies):.6f} "
          f"max={latencies[-1]:.6f}")
    for q, v in pcts.items():
        print(f"  p{q:<5}= {v:.6f}")
    print()
    print("memory:")
    print(f"  tracemalloc current : {current / 1e6:.1f} MB")
    print(f"  tracemalloc peak    : {peak / 1e6:.1f} MB")
    print(f"  ru_maxrss           : {maxrss_kb / 1024:.1f} MB")


if __name__ == "__main__":
    main()

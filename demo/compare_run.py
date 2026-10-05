"""对拍 demo: the same workload executed twice.

Run 1 ("full run")     - no shutdown; every task runs to completion.
Run 2 ("shutdown run") - shutdown() is triggered mid-flight.

The script prints the shutdown timeline, the per-task outcome table and
a per-task explanation of every difference between the two runs.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graceful_shutdown import (  # noqa: E402
    Disposition,
    GracefulExecutor,
    Task,
    compare_runs,
)

LONG_STEPS = 400          # 400 steps * 1ms ~= 0.4s per long task
QUICK_TASKS = 4
LONG_DISPOSITIONS = (
    # interleaved so the 4 tasks running at shutdown cover all 3 dispositions
    [Disposition.CHECKPOINT, Disposition.ROLLBACK, Disposition.FAIL,
     Disposition.CHECKPOINT]
    + [Disposition.CHECKPOINT, Disposition.ROLLBACK, Disposition.FAIL,
       Disposition.CHECKPOINT]
)


class QuickTask(Task):
    def run(self, cancel):
        time.sleep(0.01)


class LongTask(Task):
    def __init__(self, task_id, disposition, steps=LONG_STEPS):
        super().__init__(task_id)
        self.disposition = disposition
        self.steps = steps
        self.progress = 0
        self.rolled_back = False

    def run(self, cancel):
        for i in range(self.steps):
            self.progress = i + 1
            time.sleep(0.001)

    def checkpoint(self):
        return {"progress": self.progress, "total_steps": self.steps}

    def rollback(self):
        self.rolled_back = True


def build_tasks():
    tasks = [QuickTask(f"quick-{i}") for i in range(QUICK_TASKS)]
    tasks += [
        LongTask(f"long-{i}", disposition)
        for i, disposition in enumerate(LONG_DISPOSITIONS)
    ]
    return tasks


def full_run():
    executor = GracefulExecutor(max_workers=4)
    for task in build_tasks():
        executor.submit(task)
    assert executor.wait_idle(timeout=60), "full run did not finish"
    return executor.records_snapshot()


def shutdown_run():
    executor = GracefulExecutor(
        max_workers=4,
        grace_period=0.05,
        dispose_budget_per_task=0.5,
        hard_deadline=5.0,
        poll_interval=0.01,
    )
    for task in build_tasks():
        executor.submit(task)
    # Trigger shutdown as soon as the quick tasks are done.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        done = sum(
            1 for rec in executor.records_snapshot().values()
            if rec.outcome is not None
        )
        if done >= QUICK_TASKS:
            break
        time.sleep(0.005)
    return executor, executor.shutdown()


def main():
    print("=== run 1: full run (no shutdown) ===")
    full_records = full_run()
    print(f"all {len(full_records)} tasks completed\n")

    print("=== run 2: shutdown mid-flight ===")
    executor, report = shutdown_run()
    print("shutdown timeline (wall clock, seconds):")
    for timing in report.phases:
        print(
            f"  {timing.phase.value:<15} start={timing.start:10.4f} "
            f"end={timing.end:10.4f} duration={timing.duration:.4f}"
        )
    print(
        f"  total={report.total_duration:.4f}s "
        f"hard_deadline_budget={executor.hard_deadline:.4f}s "
        f"met_deadline={report.met_deadline}\n"
    )

    print("per-task outcomes (shutdown run):")
    for task_id in sorted(report.records):
        rec = report.records[task_id]
        extra = ""
        if rec.checkpoint_data is not None:
            extra = f" checkpoint={rec.checkpoint_data}"
        print(f"  {task_id:<9} {rec.outcome.value:<22} {rec.detail}{extra}")

    print("\n=== diff: full run vs shutdown run ===")
    diffs = compare_runs(full_records, report)
    if not diffs:
        print("no differences")
    for entry in diffs:
        print(
            f"  {entry.task_id:<9} {entry.full_outcome.value} -> "
            f"{entry.shutdown_outcome.value}\n"
            f"      reason: {entry.explanation}"
        )
    print(
        f"\n{len(diffs)}/{len(full_records)} tasks differ; "
        f"every difference is explained above."
    )


if __name__ == "__main__":
    main()

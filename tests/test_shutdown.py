"""Self tests for graceful shutdown.

Time is injected (FakeClock): every deadline-related assertion below is
on virtual time and is therefore deterministic. Worker threads are real,
so FakeClock.sleep() yields a tiny amount of real time to let them run.
"""

from __future__ import annotations

import threading
import time
import unittest

from graceful_shutdown import (
    Disposition,
    FakeClock,
    GracefulExecutor,
    Phase,
    ShutdownInProgress,
    Task,
    TaskOutcome,
    compare_runs,
)
from graceful_shutdown.executor import TaskRecord


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.001) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


class CooperativeTask(Task):
    """Finishes immediately once the cancel event is set."""

    def run(self, cancel: threading.Event) -> None:
        while not cancel.is_set():
            time.sleep(0.001)


class StuckFailTask(Task):
    """Ignores cancel and never returns; disposition is FAIL."""

    disposition = Disposition.FAIL

    def run(self, cancel: threading.Event) -> None:
        threading.Event().wait()


class CheckpointTask(Task):
    """Stuck running task whose checkpoint hook works (or hangs)."""

    disposition = Disposition.CHECKPOINT

    def __init__(self, task_id: str, hang: bool = False) -> None:
        super().__init__(task_id)
        self._hang = hang

    def run(self, cancel: threading.Event) -> None:
        threading.Event().wait()

    def checkpoint(self):
        if self._hang:
            threading.Event().wait()
        return {"task": self.task_id, "progress": 0.5}


class RaisingCheckpointTask(CheckpointTask):
    def checkpoint(self):
        raise RuntimeError("disk full")


class RollbackTask(Task):
    disposition = Disposition.ROLLBACK

    def __init__(self, task_id: str) -> None:
        super().__init__(task_id)
        self.rolled_back = False

    def run(self, cancel: threading.Event) -> None:
        threading.Event().wait()

    def rollback(self) -> None:
        self.rolled_back = True


class FailingTask(Task):
    """A task that raises while running."""

    def run(self, cancel: threading.Event) -> None:
        raise ValueError("boom")


def make_executor(tasks, max_workers=4, **kwargs):
    clock = kwargs.pop("clock", FakeClock())
    executor = GracefulExecutor(max_workers, clock=clock, **kwargs)
    for task in tasks:
        executor.submit(task)
    return executor, clock


class ShutdownTests(unittest.TestCase):
    def test_no_inflight_tasks_is_instant(self):
        clock = FakeClock()
        executor = GracefulExecutor(4, grace_period=2.0, hard_deadline=10.0, clock=clock)
        t0 = clock.now()

        report = executor.shutdown()

        self.assertTrue(report.met_deadline)
        self.assertEqual(report.total_duration, 0.0)
        self.assertEqual(report.exited_at, t0)
        self.assertEqual(report.records, {})
        self.assertEqual([p.phase for p in report.phases],
                         [Phase.STOP_ACCEPTING, Phase.WAIT_INFLIGHT,
                          Phase.FORCE_DISPOSE, Phase.EXITED])

    def test_submit_rejected_after_shutdown(self):
        clock = FakeClock()
        executor = GracefulExecutor(4, grace_period=1.0, hard_deadline=5.0, clock=clock)
        executor.shutdown()
        with self.assertRaises(ShutdownInProgress):
            executor.submit(CooperativeTask("late"))
        with self.assertRaises(RuntimeError):
            executor.shutdown()

    def test_cooperative_tasks_finish_within_grace(self):
        tasks = [CooperativeTask(f"coop-{i}") for i in range(3)]
        executor, clock = make_executor(
            tasks, max_workers=3, grace_period=2.0, hard_deadline=10.0
        )
        self.assertTrue(wait_until(lambda: executor.running_count() == 3))

        report = executor.shutdown()

        self.assertTrue(report.met_deadline)
        self.assertLessEqual(report.total_duration, 2.0)
        for task in tasks:
            self.assertEqual(report.records[task.task_id].outcome,
                             TaskOutcome.COMPLETED)
        self.assertLessEqual(
            report.phase(Phase.WAIT_INFLIGHT).duration, 2.0
        )

    def test_stuck_task_marked_failed_and_shutdown_is_punctual(self):
        task = StuckFailTask("stuck-0")
        executor, clock = make_executor(
            [task], max_workers=1, grace_period=2.0,
            dispose_budget_per_task=1.0, hard_deadline=10.0, poll_interval=0.05,
        )
        self.assertTrue(wait_until(lambda: executor.running_count() == 1))
        t0 = clock.now()

        report = executor.shutdown()

        rec = report.records["stuck-0"]
        self.assertEqual(rec.outcome, TaskOutcome.MARKED_FAILED)
        self.assertIn("disposition=fail", rec.detail)
        self.assertTrue(report.met_deadline)
        # Grace must expire fully (2.0s virtual), dispose is instant.
        self.assertAlmostEqual(report.total_duration, 2.0, places=6)
        self.assertAlmostEqual(report.exited_at, t0 + 2.0, places=6)
        self.assertEqual(
            report.phase(Phase.WAIT_INFLIGHT).duration, 2.0
        )

    def test_checkpoint_disposition_persists_state(self):
        task = CheckpointTask("cp-0")
        executor, _ = make_executor(
            [task], max_workers=1, grace_period=1.0,
            dispose_budget_per_task=2.0, hard_deadline=10.0, poll_interval=0.05,
        )
        self.assertTrue(wait_until(lambda: executor.running_count() == 1))

        report = executor.shutdown()

        rec = report.records["cp-0"]
        self.assertEqual(rec.outcome, TaskOutcome.CHECKPOINTED)
        self.assertEqual(rec.checkpoint_data,
                         {"task": "cp-0", "progress": 0.5})
        self.assertIsNotNone(rec.dispose_latency)
        self.assertLessEqual(rec.dispose_latency, 2.0)
        self.assertTrue(report.met_deadline)

    def test_rollback_disposition_undoes_side_effects(self):
        task = RollbackTask("rb-0")
        executor, _ = make_executor(
            [task], max_workers=1, grace_period=1.0,
            dispose_budget_per_task=2.0, hard_deadline=10.0, poll_interval=0.05,
        )
        self.assertTrue(wait_until(lambda: executor.running_count() == 1))

        report = executor.shutdown()

        self.assertTrue(task.rolled_back)
        rec = report.records["rb-0"]
        self.assertEqual(rec.outcome, TaskOutcome.ROLLED_BACK)
        self.assertIn("safe to retry", rec.detail)

    def test_queued_tasks_cancelled_before_start(self):
        tasks = [StuckFailTask(f"s-{i}") for i in range(5)]
        executor, _ = make_executor(
            tasks, max_workers=1, grace_period=1.0,
            dispose_budget_per_task=1.0, hard_deadline=10.0, poll_interval=0.05,
        )
        self.assertTrue(wait_until(lambda: executor.running_count() == 1))

        report = executor.shutdown()

        self.assertEqual(report.records["s-0"].outcome, TaskOutcome.MARKED_FAILED)
        for i in range(1, 5):
            rec = report.records[f"s-{i}"]
            self.assertEqual(rec.outcome, TaskOutcome.CANCELLED_BEFORE_START)
            self.assertEqual(rec.dispose_latency, 0.0)
            self.assertIsNone(rec.started_at)

    def test_hard_deadline_caps_the_grace_period(self):
        task = StuckFailTask("s-0")
        executor, clock = make_executor(
            [task], max_workers=1, grace_period=100.0,
            dispose_budget_per_task=1.0, hard_deadline=3.0, poll_interval=0.05,
        )
        self.assertTrue(wait_until(lambda: executor.running_count() == 1))
        t0 = clock.now()

        report = executor.shutdown()

        self.assertTrue(report.met_deadline)
        self.assertAlmostEqual(report.total_duration, 3.0, places=6)
        self.assertAlmostEqual(report.exited_at, t0 + 3.0, places=6)
        self.assertAlmostEqual(
            report.phase(Phase.WAIT_INFLIGHT).duration, 3.0, places=6
        )
        self.assertEqual(report.records["s-0"].outcome, TaskOutcome.MARKED_FAILED)
        self.assertIn("hard deadline exhausted", report.records["s-0"].detail)

    def test_dispose_timeout_marks_failed_but_still_exits_on_time(self):
        task = CheckpointTask("hang-cp", hang=True)
        executor, clock = make_executor(
            [task], max_workers=1, grace_period=1.0,
            dispose_budget_per_task=2.0, hard_deadline=10.0, poll_interval=0.05,
        )
        self.assertTrue(wait_until(lambda: executor.running_count() == 1))
        t0 = clock.now()

        report = executor.shutdown()

        rec = report.records["hang-cp"]
        self.assertEqual(rec.outcome, TaskOutcome.MARKED_FAILED)
        self.assertIn("exceeded budget", rec.detail)
        self.assertAlmostEqual(rec.dispose_latency, 2.0, places=6)
        self.assertTrue(report.met_deadline)
        self.assertAlmostEqual(report.exited_at, t0 + 3.0, places=6)

    def test_dispose_raising_marks_failed(self):
        task = RaisingCheckpointTask("raise-cp")
        executor, _ = make_executor(
            [task], max_workers=1, grace_period=1.0,
            dispose_budget_per_task=2.0, hard_deadline=10.0, poll_interval=0.05,
        )
        self.assertTrue(wait_until(lambda: executor.running_count() == 1))

        report = executor.shutdown()

        rec = report.records["raise-cp"]
        self.assertEqual(rec.outcome, TaskOutcome.MARKED_FAILED)
        self.assertIn("disk full", rec.detail)

    def test_deadline_exhausted_while_disposing(self):
        tasks = [CheckpointTask(f"cp-{i}", hang=True) for i in range(2)]
        executor, clock = make_executor(
            tasks, max_workers=2, grace_period=1.0,
            dispose_budget_per_task=4.0, hard_deadline=5.0, poll_interval=0.05,
        )
        self.assertTrue(wait_until(lambda: executor.running_count() == 2))
        t0 = clock.now()

        report = executor.shutdown()

        details = {report.records[f"cp-{i}"].detail for i in range(2)}
        self.assertTrue(any("exceeded budget" in d for d in details), details)
        self.assertTrue(any("hard deadline exhausted" in d for d in details),
                        details)
        for i in range(2):
            self.assertEqual(report.records[f"cp-{i}"].outcome,
                             TaskOutcome.MARKED_FAILED)
        self.assertTrue(report.met_deadline)
        self.assertAlmostEqual(report.exited_at, t0 + 5.0, places=6)
        self.assertAlmostEqual(report.total_duration, 5.0, places=6)

    def test_task_that_raises_is_recorded(self):
        task = FailingTask("boom-0")
        executor, _ = make_executor(
            [task], max_workers=1, grace_period=1.0, hard_deadline=10.0
        )
        self.assertTrue(wait_until(lambda: executor.in_flight_count() == 0))

        report = executor.shutdown()

        rec = report.records["boom-0"]
        self.assertEqual(rec.outcome, TaskOutcome.MARKED_FAILED)
        self.assertIn("boom", rec.detail)

    def test_compare_runs_explains_every_difference(self):
        full_records = {
            "cp-0": TaskRecord("cp-0", outcome=TaskOutcome.COMPLETED),
            "rb-0": TaskRecord("rb-0", outcome=TaskOutcome.COMPLETED),
            "s-0": TaskRecord("s-0", outcome=TaskOutcome.COMPLETED),
        }
        tasks = [CheckpointTask("cp-0"), RollbackTask("rb-0"),
                 StuckFailTask("s-0")]
        executor, _ = make_executor(
            tasks, max_workers=3, grace_period=1.0,
            dispose_budget_per_task=2.0, hard_deadline=10.0, poll_interval=0.05,
        )
        self.assertTrue(wait_until(lambda: executor.running_count() == 3))

        report = executor.shutdown()
        diffs = compare_runs(full_records, report)

        self.assertEqual(len(diffs), 3)
        by_id = {d.task_id: d for d in diffs}
        self.assertEqual(by_id["cp-0"].shutdown_outcome,
                         TaskOutcome.CHECKPOINTED)
        self.assertIn("checkpoint", by_id["cp-0"].explanation)
        self.assertEqual(by_id["rb-0"].shutdown_outcome,
                         TaskOutcome.ROLLED_BACK)
        self.assertIn("reverted", by_id["rb-0"].explanation)
        self.assertEqual(by_id["s-0"].shutdown_outcome,
                         TaskOutcome.MARKED_FAILED)
        self.assertIn("manual intervention", by_id["s-0"].explanation)


if __name__ == "__main__":
    unittest.main()

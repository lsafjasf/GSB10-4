"""Graceful shutdown executor.

Shutdown pipeline (every deadline is measured on the injected clock):

1. STOP_ACCEPTING  ``submit()`` starts raising ShutdownInProgress.
                     Tasks still in the queue are cancelled before they
                     ever start (no side effects). This phase is instant.
2. WAIT_INFLIGHT   The cancel event is set so cooperative tasks can
                     finish; the executor waits until no task is running
                     or the grace period ends. The grace period itself
                     is capped by the hard deadline.
3. FORCE_DISPOSE   Tasks still running are disposed one by one, in
                     start order, according to their declared
                     disposition (checkpoint / rollback / fail). Each
                     dispose attempt gets at most
                     ``dispose_budget_per_task``; the phase as a whole
                     is bounded by the hard deadline. Dispose hooks that
                     time out or raise, and tasks never reached before
                     the deadline, are marked failed.
4. EXITED          ``shutdown()`` is guaranteed to return no later
                     than ``t0 + hard_deadline`` on the injected clock.

Unfinished worker threads are daemons and never block exit.
"""

from __future__ import annotations

import copy
import threading
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque, Dict, List, Optional

from .clock import SystemClock
from .task import Disposition, Task, TaskOutcome


class Phase(str, Enum):
    STOP_ACCEPTING = "stop_accepting"
    WAIT_INFLIGHT = "wait_inflight"
    FORCE_DISPOSE = "force_dispose"
    EXITED = "exited"


class ShutdownInProgress(RuntimeError):
    """Raised by ``submit()`` once shutdown has begun."""


@dataclass
class TaskRecord:
    task_id: str
    outcome: Optional[TaskOutcome] = None
    detail: str = ""
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    dispose_latency: Optional[float] = None
    checkpoint_data: object = None


@dataclass
class PhaseTiming:
    phase: Phase
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class ShutdownReport:
    phases: List[PhaseTiming]
    records: Dict[str, TaskRecord]
    hard_deadline: float
    exited_at: float

    @property
    def started_at(self) -> float:
        return self.phases[0].start

    @property
    def total_duration(self) -> float:
        return self.exited_at - self.started_at

    @property
    def met_deadline(self) -> bool:
        return self.exited_at <= self.hard_deadline

    def phase(self, phase: Phase) -> Optional[PhaseTiming]:
        for timing in self.phases:
            if timing.phase is phase:
                return timing
        return None

    def outcome_counts(self) -> Dict[TaskOutcome, int]:
        counts: Dict[TaskOutcome, int] = {}
        for rec in self.records.values():
            counts[rec.outcome] = counts.get(rec.outcome, 0) + 1
        return counts


class GracefulExecutor:
    def __init__(
        self,
        max_workers: int = 4,
        *,
        grace_period: float = 5.0,
        dispose_budget_per_task: float = 1.0,
        hard_deadline: float = 15.0,
        clock=None,
        poll_interval: float = 0.05,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if grace_period < 0 or dispose_budget_per_task < 0 or hard_deadline <= 0:
            raise ValueError("deadlines must be non-negative / positive")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be > 0")

        self.clock = clock if clock is not None else SystemClock()
        self.grace_period = grace_period
        self.dispose_budget_per_task = dispose_budget_per_task
        self.hard_deadline = hard_deadline
        self.poll_interval = poll_interval

        self._cond = threading.Condition()
        self._queued: Deque[Task] = deque()
        self._running: Dict[str, Task] = {}
        self._records: Dict[str, TaskRecord] = {}
        self._accepting = True
        self._shutdown_started = False
        self._cancel = threading.Event()

        self._workers = [
            threading.Thread(
                target=self._worker_loop,
                name=f"graceful-worker-{i}",
                daemon=True,
            )
            for i in range(max_workers)
        ]
        for worker in self._workers:
            worker.start()

    # ------------------------------------------------------------------
    # Normal operation
    # ------------------------------------------------------------------

    def submit(self, task: Task) -> None:
        with self._cond:
            if not self._accepting:
                raise ShutdownInProgress(
                    f"executor is shutting down; rejected task {task.task_id!r}"
                )
            self._records[task.task_id] = TaskRecord(task_id=task.task_id)
            self._queued.append(task)
            self._cond.notify()

    def in_flight_count(self) -> int:
        """Accepted but not finished (queued + running)."""
        with self._cond:
            return len(self._queued) + len(self._running)

    def running_count(self) -> int:
        with self._cond:
            return len(self._running)

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Wait until everything accepted has finished (used by the
        all-completed baseline run)."""
        deadline = None if timeout is None else self.clock.now() + timeout
        while True:
            with self._cond:
                idle = not self._queued and not self._running
            if idle:
                return True
            if deadline is not None and self.clock.now() >= deadline:
                return False
            self.clock.sleep(self.poll_interval)

    def records_snapshot(self) -> Dict[str, TaskRecord]:
        with self._cond:
            return {tid: copy.copy(rec) for tid, rec in self._records.items()}

    def _worker_loop(self) -> None:
        while True:
            with self._cond:
                while not self._queued and not self._shutdown_started:
                    self._cond.wait()
                if not self._queued:
                    # Shutdown started: queue already drained by shutdown().
                    return
                task = self._queued.popleft()
                self._running[task.task_id] = task
                rec = self._records[task.task_id]
                rec.started_at = self.clock.now()
            try:
                task.run(self._cancel)
                outcome, detail = TaskOutcome.COMPLETED, "finished normally"
            except Exception as exc:  # record, don't kill the worker
                outcome, detail = TaskOutcome.MARKED_FAILED, f"raised {exc!r}"
            with self._cond:
                self._running.pop(task.task_id, None)
                if rec.outcome is None:
                    rec.outcome = outcome
                    rec.detail = detail
                    rec.finished_at = self.clock.now()
                self._cond.notify_all()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> ShutdownReport:
        t0 = self.clock.now()
        phases: List[PhaseTiming] = []
        hard_deadline = t0 + self.hard_deadline

        # Phase 1: stop accepting + snapshot/cancel everything queued.
        # Both happen in one critical section so a queued task can never
        # race into "running" during shutdown.
        with self._cond:
            if self._shutdown_started:
                raise RuntimeError("shutdown() may only be called once")
            self._shutdown_started = True
            self._accepting = False
            queued = list(self._queued)
            self._queued.clear()
            self._cond.notify_all()

        for task in queued:
            start = self.clock.now()
            rec = self._records[task.task_id]
            rec.outcome = TaskOutcome.CANCELLED_BEFORE_START
            rec.detail = "queued at shutdown; never started, no side effects"
            rec.finished_at = self.clock.now()
            rec.dispose_latency = rec.finished_at - start
        phases.append(PhaseTiming(Phase.STOP_ACCEPTING, t0, self.clock.now()))

        # Phase 2: signal cooperative tasks and wait within the grace period.
        # The grace window starts when the wait starts (cancelling queued
        # tasks does not eat into it); it is still capped by the deadline.
        wait_start = self.clock.now()
        self._cancel.set()
        grace_end = min(wait_start + self.grace_period, hard_deadline)
        self._wait_until(lambda: self._is_running_empty(), grace_end)
        phases.append(PhaseTiming(Phase.WAIT_INFLIGHT, wait_start, self.clock.now()))

        # Phase 3: force-dispose whatever is still running.
        dispose_start = self.clock.now()
        self._dispose_all(hard_deadline)
        phases.append(PhaseTiming(Phase.FORCE_DISPOSE, dispose_start, self.clock.now()))

        # Snapshot before stamping exited_at so total_duration covers
        # everything shutdown() does before returning.
        records = self.records_snapshot()
        exited_at = self.clock.now()
        phases.append(PhaseTiming(Phase.EXITED, exited_at, exited_at))
        return ShutdownReport(
            phases=phases,
            records=records,
            hard_deadline=hard_deadline,
            exited_at=exited_at,
        )

    def _is_running_empty(self) -> bool:
        with self._cond:
            return not self._running

    def _wait_until(self, predicate, deadline: float) -> bool:
        while True:
            if predicate():
                return True
            now = self.clock.now()
            if now >= deadline:
                return False
            self.clock.sleep(min(self.poll_interval, deadline - now))

    def _dispose_all(self, hard_deadline: float) -> None:
        with self._cond:
            pending = list(self._running.values())
        for task in pending:
            with self._cond:
                rec = self._records[task.task_id]
                already_done = rec.outcome is not None
            if already_done:
                continue  # finished on its own while we were disposing others
            remaining = hard_deadline - self.clock.now()
            if remaining <= 0:
                self._finalize(
                    task,
                    TaskOutcome.MARKED_FAILED,
                    "hard deadline exhausted before dispose could run",
                )
                continue
            budget = min(self.dispose_budget_per_task, remaining)
            self._dispose_one(task, budget)

    def _dispose_one(self, task: Task, budget: float) -> None:
        start = self.clock.now()
        disposition = task.disposition

        if disposition is Disposition.FAIL:
            self._finalize(
                task,
                TaskOutcome.MARKED_FAILED,
                "disposition=fail: marked failed without dispose attempt",
                dispose_latency=self.clock.now() - start,
            )
            return

        op = task.checkpoint if disposition is Disposition.CHECKPOINT else task.rollback
        status, value = self._run_with_timeout(op, budget)
        latency = self.clock.now() - start

        if status == "ok":
            if disposition is Disposition.CHECKPOINT:
                self._finalize(
                    task,
                    TaskOutcome.CHECKPOINTED,
                    "state saved to checkpoint; resumable",
                    dispose_latency=latency,
                    checkpoint_data=value,
                )
            else:
                self._finalize(
                    task,
                    TaskOutcome.ROLLED_BACK,
                    "side effects reverted; safe to retry",
                    dispose_latency=latency,
                )
        elif status == "timeout":
            self._finalize(
                task,
                TaskOutcome.MARKED_FAILED,
                f"dispose ({disposition.value}) exceeded budget {budget:.6f}s",
                dispose_latency=latency,
            )
        else:
            self._finalize(
                task,
                TaskOutcome.MARKED_FAILED,
                f"dispose ({disposition.value}) raised {value!r}",
                dispose_latency=latency,
            )

    def _run_with_timeout(self, fn, budget: float):
        box: Dict[str, object] = {}

        def target() -> None:
            try:
                box["status"] = "ok"
                box["value"] = fn()
            except Exception as exc:
                box["status"] = "error"
                box["value"] = exc

        runner = threading.Thread(target=target, daemon=True)
        runner.start()
        deadline = self.clock.now() + budget
        while runner.is_alive():
            now = self.clock.now()
            if now >= deadline:
                return "timeout", None
            self.clock.sleep(min(self.poll_interval, deadline - now))
        return box.get("status", "error"), box.get("value")

    def _finalize(
        self,
        task: Task,
        outcome: TaskOutcome,
        detail: str,
        dispose_latency: Optional[float] = None,
        checkpoint_data: object = None,
    ) -> None:
        with self._cond:
            rec = self._records[task.task_id]
            if rec.outcome is not None:
                return  # never overwrite a completed record
            rec.outcome = outcome
            rec.detail = detail
            rec.finished_at = self.clock.now()
            rec.dispose_latency = dispose_latency
            rec.checkpoint_data = checkpoint_data
            self._running.pop(task.task_id, None)
            self._cond.notify_all()

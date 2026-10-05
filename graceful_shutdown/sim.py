"""Discrete-event engine driven by a virtual clock injected by the test.

Time never advances implicitly: the test (and the shutdown coordinator)
advance it with :meth:`SimEngine.advance_until`. This makes 100k-task
shutdown scenarios deterministic and millisecond-fast while exercising the
exact same disposition/ledger code as the real threaded engine.

Cancellation is cooperative: a running task always finishes its current
step (its safe point) before disposition starts. A ``stuck`` task sits in
an infinitely long step and holds its worker forever -- it can only end as
ABANDONED at the force-exit phase.
"""
from __future__ import annotations

import heapq
from collections import deque

from .model import (
    DurableStore,
    Ledger,
    Status,
    TaskResult,
    TaskSpec,
    ResultTable,
    apply_disposition,
)


class _SimTask:
    __slots__ = ("spec", "steps_done", "state")

    def __init__(self, spec: TaskSpec) -> None:
        self.spec = spec
        self.steps_done = 0
        self.state = "queued"


class SimEngine:
    def __init__(
        self,
        workers: int = 8,
        cleanup_workers: int | None = None,
        start: float = 0.0,
    ) -> None:
        self._now = start
        self._workers_total = workers
        self._cleanup_total = workers if cleanup_workers is None else cleanup_workers
        self._free_workers = workers
        self._free_cleanup = self._cleanup_total
        self._queue: deque[_SimTask] = deque()
        self._cleanup_queue: deque[_SimTask] = deque()
        self._heap: list[tuple[float, int, str, _SimTask]] = []
        self._seq = 0
        self._tasks: dict[str, _SimTask] = {}
        self._unfinished: set[str] = set()
        self._intake_open = True
        self._cancel_requested = False
        self.events_processed = 0

        self.results = ResultTable()
        self.store = DurableStore()
        self.ledger = Ledger()

    # Engine protocol ---------------------------------------------------

    def now(self) -> float:
        return self._now

    def submit(self, spec: TaskSpec) -> bool:
        if not self._intake_open:
            self.results.finalize(
                TaskResult(spec.task_id, Status.REJECTED, 0, "intake closed")
            )
            return False
        task = _SimTask(spec)
        self._tasks[spec.task_id] = task
        self._unfinished.add(spec.task_id)
        self._queue.append(task)
        self._pump()
        return True

    def close_intake(self) -> None:
        self._intake_open = False

    def cancel_all(self) -> None:
        """Signal cancel. Queued tasks go to disposition with 0 steps done;
        running tasks go to disposition at their next safe point."""
        self._cancel_requested = True
        while self._queue:
            task = self._queue.popleft()
            task.state = "await-cleanup"
            self._cleanup_queue.append(task)
        self._pump_cleanup()

    def advance_until(self, deadline: float) -> None:
        # Nothing to wait for: return early (instant zero-in-flight shutdown).
        if not self._unfinished and not self._heap:
            return
        while self._heap and self._heap[0][0] <= deadline:
            when, _, kind, task = heapq.heappop(self._heap)
            self._now = when
            self.events_processed += 1
            if task.state == "done":
                continue  # stale event (task was abandoned at force-exit)
            if kind == "step":
                self._on_step(task)
            else:
                self._on_cleanup(task)
        # Unfinished work remains (stuck tasks, or events past the deadline):
        # burn exactly the remaining budget of this phase.
        if self._unfinished:
            self._now = max(self._now, deadline)

    def abandon_rest(self, note: str) -> list[str]:
        abandoned: list[str] = []
        for tid in sorted(self._unfinished):
            task = self._tasks[tid]
            task.state = "done"
            self.ledger.record(tid, "abandon", note)
            self.results.finalize(
                TaskResult(tid, Status.ABANDONED, task.steps_done, note)
            )
            abandoned.append(tid)
        self._unfinished.clear()
        self._queue.clear()
        self._cleanup_queue.clear()
        return abandoned

    def unfinished(self) -> list[str]:
        return sorted(self._unfinished)

    def metrics(self) -> dict:
        return {
            "engine": "sim",
            "virtual": True,
            "workers": self._workers_total,
            "cleanup_workers": self._cleanup_total,
            "events_processed": self.events_processed,
        }

    # internals ----------------------------------------------------------

    def _push(self, when: float, kind: str, task: _SimTask) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (when, self._seq, kind, task))

    def _pump(self) -> None:
        while self._free_workers > 0 and self._queue:
            task = self._queue.popleft()
            self._free_workers -= 1
            self._start(task)

    def _start(self, task: _SimTask) -> None:
        if task.spec.stuck:
            task.state = "stuck"  # infinitely long step; worker held forever
            return
        task.state = "running"
        self._schedule_step(task)

    def _schedule_step(self, task: _SimTask) -> None:
        if task.steps_done >= len(task.spec.steps):
            self._complete(task)
            return
        duration = task.spec.steps[task.steps_done]
        self._push(self._now + duration, "step", task)

    def _on_step(self, task: _SimTask) -> None:
        task.steps_done += 1
        tid = task.spec.task_id
        self.ledger.record(tid, "step_done", str(task.steps_done))
        if self._cancel_requested:
            # Safe point reached: release the execution worker and dispose.
            self._free_workers += 1
            task.state = "await-cleanup"
            self._cleanup_queue.append(task)
            self._pump_cleanup()
        elif task.steps_done >= len(task.spec.steps):
            self._complete(task)
        else:
            self._schedule_step(task)
        self._pump()

    def _complete(self, task: _SimTask) -> None:
        self._free_workers += 1
        task.state = "done"
        self.results.finalize(
            TaskResult(task.spec.task_id, Status.FINISHED, task.steps_done)
        )
        self._unfinished.discard(task.spec.task_id)

    def _pump_cleanup(self) -> None:
        while self._free_cleanup > 0 and self._cleanup_queue:
            task = self._cleanup_queue.popleft()
            self._free_cleanup -= 1
            task.state = "cleanup"
            cost = task.spec.disposition_cost(task.steps_done)
            self._push(self._now + cost, "cleanup", task)

    def _on_cleanup(self, task: _SimTask) -> None:
        self._free_cleanup += 1
        task.state = "done"
        apply_disposition(
            task.spec, task.steps_done, self.results, self.store, self.ledger
        )
        self._unfinished.discard(task.spec.task_id)
        self._pump_cleanup()

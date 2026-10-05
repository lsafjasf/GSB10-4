"""Real-time engine: daemon worker threads on :func:`time.monotonic`.

Identical shutdown semantics to :class:`~graceful_shutdown.sim.SimEngine`
(cooperative cancel at step boundaries, dedicated cleanup workers,
first-write-wins results), but driven by the wall clock. It exists to prove
the time-budget contract on a real runtime (see ``demo_real_exit.py``):
worker threads are daemons and the demo ends with ``os._exit``, so even
stuck tasks cannot keep the process alive past the deadline.
"""
from __future__ import annotations

import threading
import time
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


class _ThreadTask:
    __slots__ = ("spec", "steps_done")

    def __init__(self, spec: TaskSpec) -> None:
        self.spec = spec
        self.steps_done = 0


class ThreadEngine:
    def __init__(self, workers: int = 4, cleanup_workers: int = 2) -> None:
        self._cond = threading.Condition()
        self._queue: deque[_ThreadTask] = deque()
        self._cleanup_queue: deque[_ThreadTask] = deque()
        self._tasks: dict[str, _ThreadTask] = {}
        self._unfinished: set[str] = set()
        self._intake_open = True
        self._cancel_requested = False
        self._workers_total = workers
        self._cleanup_total = cleanup_workers

        self.results = ResultTable()
        self.store = DurableStore()
        self.ledger = Ledger()

        for _ in range(workers):
            threading.Thread(target=self._worker_loop, daemon=True).start()
        for _ in range(cleanup_workers):
            threading.Thread(target=self._cleanup_loop, daemon=True).start()

    def now(self) -> float:
        return time.monotonic()

    def submit(self, spec: TaskSpec) -> bool:
        with self._cond:
            if not self._intake_open:
                self.results.finalize(
                    TaskResult(spec.task_id, Status.REJECTED, 0, "intake closed")
                )
                return False
            task = _ThreadTask(spec)
            self._tasks[spec.task_id] = task
            self._unfinished.add(spec.task_id)
            self._queue.append(task)
            self._cond.notify_all()
            return True

    def close_intake(self) -> None:
        with self._cond:
            self._intake_open = False

    def cancel_all(self) -> None:
        with self._cond:
            self._cancel_requested = True
            while self._queue:
                self._cleanup_queue.append(self._queue.popleft())
            self._cond.notify_all()

    def advance_until(self, deadline: float) -> None:
        with self._cond:
            while self._unfinished:
                remaining = deadline - self.now()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)

    def abandon_rest(self, note: str) -> list[str]:
        abandoned: list[str] = []
        with self._cond:
            for tid in sorted(self._unfinished):
                task = self._tasks[tid]
                self.ledger.record(tid, "abandon", note)
                self.results.finalize(
                    TaskResult(tid, Status.ABANDONED, task.steps_done, note)
                )
                abandoned.append(tid)
            self._unfinished.clear()
            self._cond.notify_all()
        return abandoned

    def metrics(self) -> dict:
        return {
            "engine": "threaded",
            "virtual": False,
            "workers": self._workers_total,
            "cleanup_workers": self._cleanup_total,
        }

    # worker threads -----------------------------------------------------

    def _worker_loop(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                task = self._queue.popleft()
            self._run(task)

    def _run(self, task: _ThreadTask) -> None:
        spec = task.spec
        tid = spec.task_id
        if spec.stuck:
            threading.Event().wait()  # infinitely long step; daemon thread
            return
        for _ in spec.steps:
            # Cancel takes effect only at the next step boundary (safe point).
            if not self._sleep_step(tid, _):
                return
            with self._cond:
                if tid in self.results:  # force-exit landed mid-step
                    return
                task.steps_done += 1
                self.ledger.record(tid, "step_done", str(task.steps_done))
                if self._cancel_requested:
                    self._cleanup_queue.append(task)
                    self._cond.notify_all()
                    return
        with self._cond:
            if tid in self.results:
                return
            self.results.finalize(TaskResult(tid, Status.FINISHED, task.steps_done))
            self._unfinished.discard(tid)
            self._cond.notify_all()

    def _sleep_step(self, tid: str, duration: float) -> bool:
        """Sleep a whole step, waking in slices only to notice a force-exit."""
        end = time.monotonic() + duration
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.02, remaining))
            with self._cond:
                if tid in self.results:
                    return False

    def _cleanup_loop(self) -> None:
        while True:
            with self._cond:
                while not self._cleanup_queue:
                    self._cond.wait()
                task = self._cleanup_queue.popleft()
                tid = task.spec.task_id
                if tid in self.results:
                    continue
                cost = task.spec.disposition_cost(task.steps_done)
            if cost > 0:
                time.sleep(cost)  # cleanup time is real work, not interruptible
            with self._cond:
                if tid in self.results:  # force-exit landed during cleanup
                    continue
                apply_disposition(
                    task.spec, task.steps_done, self.results, self.store, self.ledger
                )
                self._unfinished.discard(tid)
                self._cond.notify_all()

"""Bounded thread pool with explicit rejection policies and accounting.

Stdlib only. Python 3.8+.

Conservation invariant (checked under the pool lock, holds at any
quiescent point):

    submitted == completed + rejected + queued + running

Where:
  submitted  - every submit() call ever made
  completed  - tasks that ran to the end (failed is a sub-count of these)
  rejected   - tasks that will never run:
               refused at submit (queue full / shutting down / terminated)
               + evicted by DISCARD_OLDEST
               + drained by shutdown_now()
  queued     - tasks waiting in the bounded queue right now
  running    - tasks executing right now (pool worker or caller-runs inline)
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, List, Optional, Tuple


class Policy(Enum):
    """What to do when submit() finds the queue full (pool still RUNNING)."""

    ABORT = "abort"                    # raise RejectedExecutionError at the caller
    DISCARD = "discard"                # return SubmitResult(accepted=False, reason=QUEUE_FULL)
    CALLER_RUNS = "caller_runs"        # execute the task inline on the submitting thread
    DISCARD_OLDEST = "discard_oldest"  # evict oldest queued task (counted rejected), accept new


class State(Enum):
    RUNNING = "running"          # accepts submissions
    SHUTDOWN = "shutdown"        # graceful: no new submissions, queued tasks drain
    STOP = "stop"                # shutdown_now: queued tasks cancelled, workers exit
    TERMINATED = "terminated"    # all workers exited


class RejectReason(Enum):
    QUEUE_FULL = "queue_full"                  # refused at submit, queue at capacity
    SHUTTING_DOWN = "shutting_down"            # submit while state == SHUTDOWN
    STOPPING = "stopping"                      # submit while state == STOP
    TERMINATED = "terminated"                  # submit after termination
    DISCARDED_OLDEST = "discarded_oldest"      # evicted by DISCARD_OLDEST policy
    SHUTDOWN_DRAINED = "shutdown_drained"      # cancelled by shutdown_now()


class RejectedExecutionError(RuntimeError):
    """Raised by submit() under Policy.ABORT. Carries the explicit reason."""

    def __init__(self, task_id: int, reason: RejectReason):
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"task {task_id} rejected: {reason.value}")


@dataclass(frozen=True)
class SubmitResult:
    """Explicit outcome of every submit() call. Nothing is dropped silently."""

    accepted: bool
    task_id: int
    reason: Optional[RejectReason] = None       # set iff accepted is False
    executed_inline: bool = False               # True for CALLER_RUNS executions

    @property
    def rejected(self) -> bool:
        return not self.accepted


@dataclass(frozen=True)
class RejectionRecord:
    """One entry in the pool's rejection log (observability for the caller)."""

    task_id: int
    reason: RejectReason
    detail: str


@dataclass
class _Task:
    task_id: int
    fn: Callable[..., Any]
    args: Tuple[Any, ...]
    kwargs: dict


class ThreadPool:
    def __init__(
        self,
        workers: int = 4,
        capacity: int = 100,
        policy: Policy = Policy.ABORT,
        on_rejected: Optional[Callable[[RejectionRecord], None]] = None,
        name: str = "pool",
    ):
        if workers < 1:
            raise ValueError("workers must be >= 1")
        if capacity < 0:
            raise ValueError("capacity must be >= 0")
        self.workers = workers
        self.capacity = capacity
        self.policy = policy
        self._on_rejected = on_rejected

        self._cond = threading.Condition()
        self._queue: Deque[_Task] = deque()
        self._state = State.RUNNING
        self._next_id = 0
        self._live_workers = 0

        # counters (all mutated under self._cond)
        self._submitted = 0
        self._completed = 0
        self._failed = 0          # sub-count of completed: fn raised
        self._rejected = 0
        self._running = 0
        self._reject_by_reason: Counter = Counter()
        self.rejection_log: List[RejectionRecord] = []

        self._threads = [
            threading.Thread(target=self._worker, name=f"{name}-worker-{i}", daemon=True)
            for i in range(workers)
        ]
        for t in self._threads:
            self._live_workers += 1
            t.start()

    # ------------------------------------------------------------------ submit

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> SubmitResult:
        """Submit a task. Never silent: returns SubmitResult or raises
        RejectedExecutionError (Policy.ABORT only)."""
        run_inline: Optional[_Task] = None
        with self._cond:
            self._submitted += 1
            self._next_id += 1
            task = _Task(self._next_id, fn, args, kwargs)

            if self._state is not State.RUNNING:
                reason = {
                    State.SHUTDOWN: RejectReason.SHUTTING_DOWN,
                    State.STOP: RejectReason.STOPPING,
                    State.TERMINATED: RejectReason.TERMINATED,
                }[self._state]
                result = self._reject_locked(task, reason, f"pool state={self._state.value}")
                if self.policy is Policy.ABORT:
                    raise RejectedExecutionError(task.task_id, reason)
                return result

            if len(self._queue) >= self.capacity:
                if self.policy is Policy.ABORT:
                    self._reject_locked(task, RejectReason.QUEUE_FULL, "policy=abort")
                    raise RejectedExecutionError(task.task_id, RejectReason.QUEUE_FULL)
                if self.policy is Policy.DISCARD:
                    return self._reject_locked(task, RejectReason.QUEUE_FULL, "policy=discard")
                if self.policy is Policy.DISCARD_OLDEST and self._queue:
                    oldest = self._queue.popleft()
                    self._reject_locked(
                        oldest, RejectReason.DISCARDED_OLDEST,
                        f"evicted by task {task.task_id}",
                    )
                    self._queue.append(task)
                    self._cond.notify()
                    return SubmitResult(accepted=True, task_id=task.task_id)
                if self.policy is Policy.CALLER_RUNS:
                    # count as running while the caller executes it, so the
                    # conservation invariant holds even mid-execution
                    self._running += 1
                    run_inline = task
                else:  # DISCARD_OLDEST with empty queue (capacity == 0)
                    return self._reject_locked(task, RejectReason.QUEUE_FULL,
                                               "policy=discard_oldest, queue empty")
            else:
                self._queue.append(task)
                self._cond.notify()
                return SubmitResult(accepted=True, task_id=task.task_id)

        # CALLER_RUNS: execute outside the lock, on the caller's thread
        assert run_inline is not None
        self._execute(run_inline)
        return SubmitResult(accepted=True, task_id=run_inline.task_id, executed_inline=True)

    def _reject_locked(self, task: _Task, reason: RejectReason, detail: str) -> SubmitResult:
        self._rejected += 1
        self._reject_by_reason[reason] += 1
        record = RejectionRecord(task.task_id, reason, detail)
        self.rejection_log.append(record)
        if self._on_rejected is not None:
            try:
                self._on_rejected(record)
            except Exception:
                pass  # observer callbacks must never break the pool
        return SubmitResult(accepted=False, task_id=task.task_id, reason=reason)

    # ------------------------------------------------------------------ workers

    def _worker(self) -> None:
        while True:
            with self._cond:
                while True:
                    if self._queue and self._state in (State.RUNNING, State.SHUTDOWN):
                        task = self._queue.popleft()
                        self._running += 1
                        break
                    # graceful: exit once drained; stop: exit immediately
                    if self._state is State.STOP or (
                        self._state is State.SHUTDOWN and not self._queue
                    ):
                        self._live_workers -= 1
                        if self._live_workers == 0:
                            self._state = State.TERMINATED
                        self._cond.notify_all()
                        return
                    self._cond.wait()
            self._execute(task)

    def _execute(self, task: _Task) -> None:
        try:
            task.fn(*task.args, **task.kwargs)
        except Exception:
            with self._cond:
                self._failed += 1
        finally:
            with self._cond:
                self._completed += 1
                self._running -= 1
                self._cond.notify_all()

    # ------------------------------------------------------------------ shutdown

    def shutdown(self, wait: bool = True) -> None:
        """Graceful: refuse new submissions, let queued tasks finish."""
        with self._cond:
            if self._state is State.RUNNING:
                self._state = State.SHUTDOWN
                self._cond.notify_all()
        if wait:
            self.wait_terminated()

    def shutdown_now(self) -> List[int]:
        """Immediate: cancel every queued task (counted as rejected with
        reason SHUTDOWN_DRAINED), let running tasks finish, stop workers.
        Returns ids of the cancelled tasks."""
        with self._cond:
            if self._state in (State.STOP, State.TERMINATED):
                return []
            self._state = State.STOP
            drained = list(self._queue)
            self._queue.clear()
            cancelled_ids = []
            for task in drained:
                self._reject_locked(task, RejectReason.SHUTDOWN_DRAINED,
                                    "cancelled by shutdown_now()")
                cancelled_ids.append(task.task_id)
            self._cond.notify_all()
            return cancelled_ids

    def wait_terminated(self, timeout: Optional[float] = None) -> bool:
        with self._cond:
            return self._cond.wait_for(
                lambda: self._state is State.TERMINATED, timeout=timeout
            )

    # ------------------------------------------------------------------ accounting

    @property
    def state(self) -> State:
        with self._cond:
            return self._state

    def accounting(self) -> dict:
        """Snapshot of the conservation ledger."""
        with self._cond:
            queued = len(self._queue)
            return {
                "state": self._state.value,
                "submitted": self._submitted,
                "completed": self._completed,
                "failed": self._failed,
                "rejected": self._rejected,
                "queued": queued,
                "running": self._running,
                "reject_by_reason": {
                    r.value: n for r, n in self._reject_by_reason.items()
                },
                # submitted == completed + rejected + queued + running
                "conserved": self._submitted
                == self._completed + self._rejected + queued + self._running,
            }

    def check_conservation(self) -> bool:
        return self.accounting()["conserved"]

    def __enter__(self) -> "ThreadPool":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.shutdown(wait=True)

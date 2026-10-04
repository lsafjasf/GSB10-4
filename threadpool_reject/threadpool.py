"""Thread pool with explicit rejection policies and caller notification.

Stdlib only (Python 3.8+).

Key guarantees:
  * submit() NEVER silently drops a task. Every call returns a SubmitResult
    that is either accepted (with a Future) or rejected (with a RejectReason).
  * Counting is conserved at all times:
        submitted == completed + rejected + running + queued
  * Submissions during/after shutdown are explicitly rejected with a reason
    that identifies the shutdown phase.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Deque, Optional, Tuple


class Policy(Enum):
    """What to do when the work queue is full."""

    ABORT = "abort"                      # reject the new task, tell the caller
    CALLER_RUNS = "caller_runs"          # caller thread executes the task itself
    DISCARD_OLDEST = "discard_oldest"    # drop the oldest queued task (its Future
                                         # gets RejectedExecution), accept the new one
    DISCARD = "discard"                  # reject the new task, tell the caller
                                         # (same accounting as ABORT, no exception
                                         #  raised by submit() in try_submit style)


class RejectReason(Enum):
    QUEUE_FULL = "queue_full"                    # policy rejected: queue at capacity
    DISCARDED_BY_POLICY = "discarded_by_policy"  # evicted by DISCARD_OLDEST
    SHUTTING_DOWN = "shutting_down"              # shutdown() called, queue draining
    SHUTDOWN = "shutdown"                        # queue drained / shutdown_now, workers exiting
    TERMINATED = "terminated"                    # all workers exited


class State(Enum):
    RUNNING = 0
    SHUTTING_DOWN = 1   # shutdown() called; queued tasks still run to completion
    STOP = 2            # queue empty (or shutdown_now); workers exit after current task
    TERMINATED = 3      # every worker thread has exited


class RejectedExecution(Exception):
    """Raised inside Future.result() of a task that was rejected/discarded."""

    def __init__(self, reason: RejectReason, message: str = ""):
        self.reason = reason
        super().__init__(message or f"task rejected: {reason.value}")


class Future:
    """Minimal future for a submitted task."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._result: Any = None
        self._exc: Optional[BaseException] = None

    def done(self) -> bool:
        return self._event.is_set()

    def result(self, timeout: Optional[float] = None) -> Any:
        if not self._event.wait(timeout):
            raise TimeoutError("future result not ready")
        if self._exc is not None:
            raise self._exc
        return self._result

    def exception(self, timeout: Optional[float] = None) -> Optional[BaseException]:
        if not self._event.wait(timeout):
            raise TimeoutError("future result not ready")
        return self._exc

    def _set_result(self, value: Any) -> None:
        self._result = value
        self._event.set()

    def _set_exception(self, exc: BaseException) -> None:
        self._exc = exc
        self._event.set()


@dataclass(frozen=True)
class SubmitResult:
    """Explicit outcome of one submit() call. Rejection is a first-class result."""

    accepted: bool
    future: Optional[Future] = None
    reason: Optional[RejectReason] = None       # set iff accepted is False
    policy: Optional[Policy] = None             # policy that produced this outcome
    executed_by_caller: bool = False            # True for CALLER_RUNS executions


@dataclass(frozen=True)
class PoolStats:
    submitted: int
    completed: int
    rejected: int
    running: int
    queued: int
    caller_runs: int
    state: State

    @property
    def conserved(self) -> bool:
        return self.submitted == self.completed + self.rejected + self.running + self.queued


class ThreadPool:
    def __init__(self, workers: int = 4, capacity: int = 100,
                 policy: Policy = Policy.ABORT) -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        if capacity < 0:
            raise ValueError("capacity must be >= 0")
        self._capacity = capacity
        self._policy = policy
        self._cond = threading.Condition()
        self._queue: Deque[Tuple[Future, Callable, tuple, dict]] = deque()
        self._state = State.RUNNING
        self._submitted = 0
        self._completed = 0
        self._rejected = 0
        self._running = 0
        self._caller_runs = 0
        self._exited = 0
        self._threads = [
            threading.Thread(target=self._worker, name=f"pool-worker-{i}", daemon=True)
            for i in range(workers)
        ]
        for t in self._threads:
            t.start()

    # ------------------------------------------------------------------ submit

    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> SubmitResult:
        """Submit a task. Always returns an explicit accepted/rejected outcome."""
        run_in_caller = False
        with self._cond:
            self._submitted += 1

            if self._state is not State.RUNNING:
                self._rejected += 1
                reason = {
                    State.SHUTTING_DOWN: RejectReason.SHUTTING_DOWN,
                    State.STOP: RejectReason.SHUTDOWN,
                    State.TERMINATED: RejectReason.TERMINATED,
                }[self._state]
                return SubmitResult(False, reason=reason, policy=self._policy)

            if len(self._queue) < self._capacity:
                fut = Future()
                self._queue.append((fut, fn, args, kwargs))
                self._cond.notify()
                return SubmitResult(True, future=fut, policy=self._policy)

            # queue is full -> apply policy
            if self._policy in (Policy.ABORT, Policy.DISCARD):
                self._rejected += 1
                return SubmitResult(False, reason=RejectReason.QUEUE_FULL,
                                    policy=self._policy)

            if self._policy is Policy.DISCARD_OLDEST:
                old_fut, _fn, _a, _k = self._queue.popleft()
                self._rejected += 1
                old_fut._set_exception(RejectedExecution(
                    RejectReason.DISCARDED_BY_POLICY,
                    "evicted from queue by discard_oldest policy"))
                fut = Future()
                self._queue.append((fut, fn, args, kwargs))
                self._cond.notify()
                return SubmitResult(True, future=fut, policy=self._policy)

            # CALLER_RUNS: run outside the lock, in the caller's thread
            self._caller_runs += 1
            run_in_caller = True

        if run_in_caller:
            fut = Future()
            try:
                fut._set_result(fn(*args, **kwargs))
            except BaseException as exc:
                fut._set_exception(exc)
            with self._cond:
                self._completed += 1
                self._cond.notify_all()
            return SubmitResult(True, future=fut, policy=self._policy,
                                executed_by_caller=True)
        raise AssertionError("unreachable")

    # ----------------------------------------------------------------- workers

    def _worker(self) -> None:
        while True:
            with self._cond:
                while not self._queue and self._state is State.RUNNING:
                    self._cond.wait()
                if not self._queue:
                    # state != RUNNING and nothing left to drain -> exit
                    self._exited += 1
                    if self._exited == len(self._threads):
                        self._state = State.TERMINATED
                    self._cond.notify_all()
                    return
                fut, fn, args, kwargs = self._queue.popleft()
                self._running += 1
            try:
                fut._set_result(fn(*args, **kwargs))
            except BaseException as exc:
                fut._set_exception(exc)
            with self._cond:
                self._running -= 1
                self._completed += 1
                self._maybe_stop()
                self._cond.notify_all()

    def _maybe_stop(self) -> None:
        """SHUTTING_DOWN -> STOP once the queue is drained and nothing runs."""
        if (self._state is State.SHUTTING_DOWN
                and not self._queue and self._running == 0):
            self._state = State.STOP
            self._cond.notify_all()

    # ---------------------------------------------------------------- shutdown

    def shutdown(self, wait: bool = True) -> None:
        """Graceful shutdown: stop accepting, drain the queue, exit workers."""
        with self._cond:
            if self._state is State.RUNNING:
                self._state = State.SHUTTING_DOWN
                self._maybe_stop()
                self._cond.notify_all()
            if wait:
                while self._state is not State.TERMINATED:
                    self._cond.wait()

    def shutdown_now(self, wait: bool = True) -> int:
        """Immediate shutdown: queued tasks are rejected (their Futures raise
        RejectedExecution), running tasks finish, workers exit.
        Returns the number of queued tasks that were rejected."""
        drained = 0
        with self._cond:
            if self._state in (State.RUNNING, State.SHUTTING_DOWN):
                self._state = State.STOP
                while self._queue:
                    fut, _fn, _a, _k = self._queue.popleft()
                    fut._set_exception(RejectedExecution(
                        RejectReason.SHUTDOWN,
                        "cancelled by shutdown_now()"))
                    self._rejected += 1
                    drained += 1
                self._cond.notify_all()
            if wait:
                while self._state is not State.TERMINATED:
                    self._cond.wait()
        return drained

    # ------------------------------------------------------------------- stats

    def stats(self) -> PoolStats:
        with self._cond:
            return PoolStats(
                submitted=self._submitted,
                completed=self._completed,
                rejected=self._rejected,
                running=self._running,
                queued=len(self._queue),
                caller_runs=self._caller_runs,
                state=self._state,
            )

    @property
    def state(self) -> State:
        with self._cond:
            return self._state

    def __enter__(self) -> "ThreadPool":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.shutdown(wait=True)

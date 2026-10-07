"""Asynchronous priority queue with aging-based starvation prevention.

Standard library only. Time is injectable through a ``clock`` callable so
tests can drive a deterministic fake clock.

Fairness model
--------------
Each task has a fixed base ``priority`` (larger = more urgent). While waiting
its *effective* priority grows linearly with its waiting time::

    eff(task, now) = base + (now - enqueued_at) / aging_interval

The scheduler always dequeues the task with the largest effective priority.
Comparing two tasks at the same instant, ``now / aging_interval`` is a common
term, so the ordering is exactly the ordering of the static, time-independent
heap key::

    key(task) = enqueued_at - base * aging_interval          (min-key heap)

This gives O(log n) ageing without ever re-heapifying.

Bounds (see FAIRNESS.md for the derivation):
* A task with base priority p, enqueued at t0, can only be overtaken by tasks
  enqueued before t0 + (P_max - p) * aging_interval. No task arriving after
  that instant can ever pass it -> bounded overtaking window.
* Under a stable load (service rate strictly above arrival rate) every task
  is dequeued in finite time; for two levels and service time s the wait is at
  most (P_high - p_low) * aging_interval + s (non-preemptive, one task may be
  mid-flight).
* Equal base priority -> keys differ only by enqueue time, so FIFO within a
  priority level is exact (a global sequence number breaks ties for tasks
  enqueued at the identical injected timestamp).
* Cancellation and timeout use lazy tombstones that are removed on dequeue;
  the heap itself is never mutated, so surviving tasks keep their relative
  order.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

__all__ = ["Task", "PriorityQueue", "AsyncPriorityQueue"]


@dataclass
class Task:
    task_id: int
    priority: int
    payload: Any = None
    deadline: Optional[float] = None
    enqueued_at: float = 0.0
    cancelled: bool = field(default=False, repr=False)
    _seq: int = field(default=0, repr=False, compare=False)


class PriorityQueue:
    """Synchronous core: all operations take an explicit ``now`` if desired."""

    def __init__(
        self,
        aging_interval: float = 100.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if aging_interval <= 0:
            raise ValueError("aging_interval must be positive")
        self._aging = float(aging_interval)
        self._clock = clock
        self._heap: list[tuple[float, int, Task]] = []
        self._seq = itertools.count()
        self._index: dict[int, Task] = {}
        self._live = 0

    def __len__(self) -> int:
        return self._live

    @property
    def heap_size(self) -> int:
        """Physical heap length (includes not-yet-swept tombstones)."""
        return len(self._heap)

    def effective_priority(self, task: Task, now: float) -> float:
        return task.priority + (now - task.enqueued_at) / self._aging

    def _key(self, task: Task) -> float:
        return task.enqueued_at - task.priority * self._aging

    def enqueue(self, task: Task, now: Optional[float] = None) -> Task:
        if now is None:
            now = self._clock()
        if task.task_id in self._index:
            raise ValueError(f"duplicate task_id: {task.task_id}")
        task.enqueued_at = now
        task.cancelled = False
        task._seq = next(self._seq)
        heapq.heappush(self._heap, (self._key(task), task._seq, task))
        self._index[task.task_id] = task
        self._live += 1
        return task

    def cancel(self, task_id: int) -> bool:
        """Mark a task cancelled. Returns True if it was live.

        Tombstones are swept lazily at dequeue time; the heap structure is
        never touched, so every other task keeps its position.
        """
        task = self._index.get(task_id)
        if task is None or task.cancelled:
            return False
        task.cancelled = True
        self._index.pop(task_id, None)
        self._live -= 1
        return True

    def dequeue(self, now: Optional[float] = None) -> Optional[Task]:
        """Pop the highest-effective-priority live task, or None.

        Cancelled tasks and tasks whose deadline has passed are dropped.
        """
        if now is None:
            now = self._clock()
        while self._heap:
            _key, _seq, task = heapq.heappop(self._heap)
            if task.cancelled:
                continue
            if task.deadline is not None and now >= task.deadline:
                self._index.pop(task.task_id, None)
                self._live -= 1
                continue
            self._index.pop(task.task_id, None)
            self._live -= 1
            return task
        return None

    def sweep(self, now: Optional[float] = None) -> int:
        """Force removal of all currently-expired/cancelled tombstones.

        Returns the number removed. Useful for memory control when many
        cancellations happen without matching dequeues.
        """
        if now is None:
            now = self._clock()
        kept: list[tuple[float, int, Task]] = []
        removed = 0
        while self._heap:
            key, seq, task = heapq.heappop(self._heap)
            if task.cancelled:
                removed += 1
            elif task.deadline is not None and now >= task.deadline:
                self._index.pop(task.task_id, None)
                self._live -= 1
                removed += 1
            else:
                kept.append((key, seq, task))
        self._heap = kept
        heapq.heapify(self._heap)
        return removed


class AsyncPriorityQueue:
    """asyncio facade. Time is still injectable via ``clock``."""

    def __init__(
        self,
        aging_interval: float = 100.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._q = PriorityQueue(aging_interval, clock)
        self._wakeup = asyncio.Event()

    async def enqueue(self, task: Task, now: Optional[float] = None) -> Task:
        result = self._q.enqueue(task, now)
        self._wakeup.set()
        return result

    def cancel(self, task_id: int) -> bool:
        return self._q.cancel(task_id)

    async def dequeue(self, now: Optional[float] = None) -> Task:
        while True:
            task = self._q.dequeue(now)
            if task is not None:
                if len(self._q) == 0:
                    self._wakeup.clear()
                return task
            self._wakeup.clear()
            await self._wakeup.wait()

    def dequeue_nowait(self, now: Optional[float] = None) -> Optional[Task]:
        return self._q.dequeue(now)

    def __len__(self) -> int:
        return len(self._q)

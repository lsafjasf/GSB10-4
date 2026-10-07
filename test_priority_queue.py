"""Deterministic unit tests for the aging priority queue (stdlib unittest).

All time is injected through FakeClock; nothing depends on wall time.
"""

import asyncio
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from priority_queue import AsyncPriorityQueue, PriorityQueue, Task


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


AGING = 100.0
P_HIGH = 10
P_LOW = 0


def make_queue(clock):
    return PriorityQueue(aging_interval=AGING, clock=clock)


class TestSinglePriority(unittest.TestCase):
    def test_fifo_completion_order(self):
        clock = FakeClock()
        q = make_queue(clock)
        for i in range(100):
            q.enqueue(Task(task_id=i, priority=5))
            clock.advance(1.0)
        order = [q.dequeue().task_id for _ in range(100)]
        self.assertEqual(order, list(range(100)))

    def test_fifo_with_identical_timestamps(self):
        # Same priority AND same injected timestamp -> global sequence breaks
        # ties, still strict FIFO.
        clock = FakeClock()
        q = make_queue(clock)
        for i in range(1000):
            q.enqueue(Task(task_id=i, priority=3))
        order = [q.dequeue(now=0.0).task_id for _ in range(1000)]
        self.assertEqual(order, list(range(1000)))

    def test_empty_dequeue_returns_none(self):
        q = make_queue(FakeClock())
        self.assertIsNone(q.dequeue())
        self.assertEqual(len(q), 0)


class TestTwoLevelFlood(unittest.TestCase):
    """High-priority flood must not starve the low-priority task."""

    def test_low_task_completes_within_bound(self):
        # Deterministic worst case: one high task arrives per unit of service
        # time (saturated link, lambda == mu == 1), starting before the low
        # task. Service time s = 1. Theoretical bound on the low task's wait:
        #   W <= (P_HIGH - P_LOW) * AGING + 2*s  = 1000 + 2
        clock = FakeClock()
        q = make_queue(clock)
        service = 1.0
        bound = (P_HIGH - P_LOW) * AGING + 2 * service

        low = q.enqueue(Task(task_id=-1, priority=P_LOW))
        next_high = 0
        completed_at = None
        # Simulate: at each step a new high task arrives, then one task serves.
        while completed_at is None and clock.t < 10 * bound:
            q.enqueue(Task(task_id=next_high, priority=P_HIGH))
            next_high += 1
            task = q.dequeue()
            clock.advance(service)
            if task.task_id == -1:
                completed_at = clock.t
        self.assertIsNotNone(completed_at, "low task starved")
        self.assertLessEqual(completed_at - low.enqueued_at, bound)

    def test_overtaking_window_is_bounded(self):
        # No task enqueued after t0 + (P_HIGH - P_LOW) * AGING may be served
        # before the low task, ever.
        clock = FakeClock()
        q = make_queue(clock)
        window = (P_HIGH - P_LOW) * AGING
        low = q.enqueue(Task(task_id=-1, priority=P_LOW))
        t0 = low.enqueued_at
        next_high = 0
        while clock.t < t0 + window + 500:
            q.enqueue(Task(task_id=next_high, priority=P_HIGH))
            next_high += 1
            task = q.dequeue()
            if task.task_id == -1:
                break
            served_enqueued_at = task.enqueued_at
            self.assertLess(
                served_enqueued_at, t0 + window,
                "a task enqueued after the overtaking window jumped ahead",
            )
            clock.advance(1.0)
        else:
            self.fail("low task not served within window + drain time")

    def test_aging_effective_priority_monotonic(self):
        # Aging lets a waiting low task overtake high tasks that ARRIVE LATER
        # than the window (it never reorders tasks already queued together,
        # which all age at the same rate).
        clock = FakeClock()
        q = make_queue(clock)
        low = q.enqueue(Task(task_id=1, priority=P_LOW))
        clock.advance((P_HIGH - P_LOW) * AGING + 1)
        high = q.enqueue(Task(task_id=2, priority=P_HIGH))  # arrives late
        self.assertGreater(q.effective_priority(low, clock.t),
                           q.effective_priority(high, clock.t))
        self.assertEqual(q.dequeue().task_id, 1)


class TestAllSamePriority(unittest.TestCase):
    def test_interleaved_enqueue_dequeue_fifo(self):
        clock = FakeClock()
        q = make_queue(clock)
        rng = random.Random(42)
        expected = []
        next_id = 0
        for _ in range(2000):
            if rng.random() < 0.6 or not expected:
                q.enqueue(Task(task_id=next_id, priority=7))
                expected.append(next_id)
                next_id += 1
                clock.advance(rng.random() * 5)
            else:
                got = q.dequeue()
                self.assertEqual(got.task_id, expected.pop(0))
        while expected:
            self.assertEqual(q.dequeue().task_id, expected.pop(0))


class TestCancellationStorm(unittest.TestCase):
    def test_continuous_cancellation_preserves_order(self):
        clock = FakeClock()
        q = make_queue(clock)
        rng = random.Random(7)
        cancelled = set()
        survivors = []
        enqueued_at = {}
        priority = {}
        next_id = 0
        # Continuously cancel ~40% of tasks while enqueueing.
        for _ in range(5000):
            pri = rng.randint(0, 9)
            q.enqueue(Task(task_id=next_id, priority=pri))
            enqueued_at[next_id] = clock.t
            priority[next_id] = pri
            survivors.append(next_id)
            next_id += 1
            clock.advance(rng.random() * 3)
            if survivors and rng.random() < 0.4:
                victim = rng.choice(survivors)
                if q.cancel(victim):
                    cancelled.add(victim)
                    survivors.remove(victim)
        # Drain: cancelled tasks must never appear.
        got = []
        while True:
            task = q.dequeue()
            if task is None:
                break
            got.append(task.task_id)
        self.assertTrue(all(t not in cancelled for t in got))
        self.assertEqual(set(got), set(survivors))
        # Survivors must come out exactly in static-key order
        # (enqueued_at - priority * AGING, insertion sequence == task_id).
        expected = sorted(
            survivors,
            key=lambda i: (enqueued_at[i] - priority[i] * AGING, i),
        )
        self.assertEqual(got, expected)

    def test_cancel_is_idempotent_and_safe(self):
        q = make_queue(FakeClock())
        q.enqueue(Task(task_id=1, priority=1))
        self.assertTrue(q.cancel(1))
        self.assertFalse(q.cancel(1))
        self.assertFalse(q.cancel(999))
        self.assertIsNone(q.dequeue())
        self.assertEqual(len(q), 0)

    def test_cancel_does_not_disturb_heap(self):
        clock = FakeClock()
        q = make_queue(clock)
        ids = list(range(200))
        for i in ids:
            q.enqueue(Task(task_id=i, priority=i % 5))
            clock.advance(0.5)
        for i in ids[::2]:
            q.cancel(i)
        got = [q.dequeue().task_id for _ in range(100)]
        self.assertEqual(got, sorted(ids[1::2],
                                     key=lambda i: (i * 0.5 - (i % 5) * AGING, i)))


class TestTimeout(unittest.TestCase):
    def test_expired_tasks_dropped_others_unaffected(self):
        clock = FakeClock()
        q = make_queue(clock)
        q.enqueue(Task(task_id=1, priority=9, deadline=50.0))
        q.enqueue(Task(task_id=2, priority=1))
        clock.advance(1.0)
        q.enqueue(Task(task_id=3, priority=9, deadline=200.0))
        clock.advance(100.0)  # task 1 expired, task 3 still valid
        self.assertEqual(q.dequeue().task_id, 3)
        self.assertEqual(q.dequeue().task_id, 2)
        self.assertIsNone(q.dequeue())
        self.assertEqual(len(q), 0)

    def test_sweep_reclaims_tombstones(self):
        clock = FakeClock()
        q = make_queue(clock)
        for i in range(100):
            q.enqueue(Task(task_id=i, priority=1, deadline=10.0 if i % 2 else None))
        for i in range(0, 100, 4):
            q.cancel(i)
        clock.advance(20.0)
        removed = q.sweep()
        self.assertGreater(removed, 0)
        self.assertEqual(q.heap_size, len(q))
        while q.dequeue() is not None:
            pass
        self.assertEqual(len(q), 0)


class TestMisc(unittest.TestCase):
    def test_duplicate_id_rejected(self):
        q = make_queue(FakeClock())
        q.enqueue(Task(task_id=1, priority=1))
        with self.assertRaises(ValueError):
            q.enqueue(Task(task_id=1, priority=2))

    def test_invalid_aging_interval(self):
        with self.assertRaises(ValueError):
            PriorityQueue(aging_interval=0)

    def test_async_facade(self):
        async def main():
            clock = FakeClock()
            q = AsyncPriorityQueue(aging_interval=AGING, clock=clock)
            await q.enqueue(Task(task_id=1, priority=0))
            await q.enqueue(Task(task_id=2, priority=5))
            first = await q.dequeue()
            self.assertEqual(first.task_id, 2)
            second = await q.dequeue()
            self.assertEqual(second.task_id, 1)
            # A waiter wakes up when a task arrives later.
            async def delayed():
                await asyncio.sleep(0.01)
                await q.enqueue(Task(task_id=3, priority=0))
            waiter = asyncio.create_task(q.dequeue())
            asyncio.create_task(delayed())
            third = await waiter
            self.assertEqual(third.task_id, 3)
        asyncio.run(main())


if __name__ == "__main__":
    unittest.main(verbosity=2)

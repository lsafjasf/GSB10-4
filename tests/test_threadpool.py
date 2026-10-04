"""Self-tests for the bounded thread pool rejection/notification behaviour."""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from threadpool import (  # noqa: E402
    Policy,
    RejectedExecutionError,
    RejectReason,
    ThreadPool,
)


class TaskLatch:
    """Blockable task: blocks until release() is called, records run order."""

    def __init__(self, order, tag=""):
        self.order = order
        self.tag = tag
        self.release = threading.Event()
        self.started = threading.Event()
        self.ran = []
        self.ran_lock = threading.Lock()

    def __call__(self):
        with self.ran_lock:
            self.ran.append((self.tag, threading.current_thread().name))
        self.started.set()
        self.release.wait(5)


class PoolTestBase(unittest.TestCase):
    def assertConserved(self, pool):
        a = pool.accounting()
        self.assertTrue(
            a["conserved"],
            f"conservation broken: {a}",
        )
        self.assertEqual(
            a["submitted"],
            a["completed"] + a["rejected"] + a["queued"] + a["running"],
            f"ledger mismatch: {a}",
        )
        return a


class TestQueueNotFull(PoolTestBase):
    def test_all_accepted_none_rejected(self):
        order = []
        order_lock = threading.Lock()
        done = threading.Event()

        def task(n):
            with order_lock:
                order.append(n)

        with ThreadPool(workers=2, capacity=5, policy=Policy.ABORT) as pool:
            for i in range(4):  # 2 running + 2 queued, below capacity
                r = pool.submit(task, i)
                self.assertTrue(r.accepted, f"task {i} should be accepted")
                self.assertIsNone(r.reason)
            self.assertConserved(pool)

        self.assertEqual(sorted(order), [0, 1, 2, 3])
        a = self.assertConserved(pool)
        self.assertEqual(a["submitted"], 4)
        self.assertEqual(a["completed"], 4)
        self.assertEqual(a["rejected"], 0)
        self.assertEqual(a["queued"], 0)
        self.assertEqual(a["running"], 0)

    def test_failed_task_still_counts_as_completed(self):
        def boom():
            raise ValueError("boom")

        with ThreadPool(workers=1, capacity=1) as pool:
            r = pool.submit(boom)
            self.assertTrue(r.accepted)
            pool.shutdown()
        a = pool.accounting()
        self.assertEqual(a["completed"], 1)
        self.assertEqual(a["failed"], 1)
        self.assertTrue(a["conserved"])


class TestQueueFull(PoolTestBase):
    def _fill_pool(self, policy):
        pool = ThreadPool(workers=2, capacity=3, policy=policy)
        tasks = [TaskLatch(i, tag=f"t{i}") for i in range(5)]  # 2 run + 3 queue
        # occupy both workers first so the queue fills deterministically
        for t in tasks[:2]:
            self.assertTrue(pool.submit(t).accepted)
        tasks[0].started.wait(2)
        tasks[1].started.wait(2)
        for t in tasks[2:]:
            self.assertTrue(pool.submit(t).accepted)
        self.assertEqual(pool.accounting()["queued"], 3)
        self.assertConserved(pool)
        return pool, tasks

    def _drain(self, pool, tasks):
        for t in tasks:
            t.release.set()
        pool.shutdown()
        self.assertConserved(pool)

    def test_abort_raises_with_reason(self):
        pool, tasks = self._fill_pool(Policy.ABORT)
        with self.assertRaises(RejectedExecutionError) as ctx:
            pool.submit(lambda: None)
        self.assertIs(ctx.exception.reason, RejectReason.QUEUE_FULL)
        self.assertEqual(ctx.exception.task_id, 6)
        self.assertIn("queue_full", str(ctx.exception))
        a = self.assertConserved(pool)
        self.assertEqual(a["rejected"], 1)
        self.assertEqual(a["reject_by_reason"]["queue_full"], 1)
        self._drain(pool, tasks)

    def test_discard_returns_explicit_result(self):
        pool, tasks = self._fill_pool(Policy.DISCARD)
        r = pool.submit(lambda: None)
        self.assertFalse(r.accepted)
        self.assertIs(r.reason, RejectReason.QUEUE_FULL)
        a = pool.accounting()
        self.assertEqual(a["rejected"], 1)
        self.assertTrue(a["conserved"])
        self._drain(pool, tasks)

    def test_caller_runs_executes_on_submitting_thread(self):
        pool, tasks = self._fill_pool(Policy.CALLER_RUNS)
        where = []
        lock = threading.Lock()
        caller = threading.current_thread().name

        r = pool.submit(lambda: where.append(threading.current_thread().name)
                        or time.sleep(0))
        self.assertTrue(r.accepted)
        self.assertTrue(r.executed_inline)
        self.assertEqual(where, [caller])  # ran on caller thread, not a worker
        a = self.assertConserved(pool)
        self.assertEqual(a["submitted"], 6)
        self.assertEqual(a["completed"], 1)
        self.assertEqual(a["rejected"], 0)
        self.assertEqual(a["queued"] + a["running"], 5)
        self._drain(pool, tasks)
        self.assertEqual(pool.accounting()["completed"], 6)

    def test_discard_oldest_evicts_head_and_records_it(self):
        pool, tasks = self._fill_pool(Policy.DISCARD_OLDEST)
        # queued order is t2, t3, t4 -> oldest is t2
        extra = TaskLatch(99, tag="t99")
        r = pool.submit(extra)
        self.assertTrue(r.accepted)
        # evicted task is observable in the rejection log with a reason
        rec = pool.rejection_log[-1]
        self.assertIs(rec.reason, RejectReason.DISCARDED_OLDEST)
        self.assertEqual(rec.task_id, 3)  # t2 was the 3rd submission
        self.assertIn("evicted by task 6", rec.detail)
        self.assertConserved(pool)

        for t in tasks:
            t.release.set()
        extra.release.set()
        pool.shutdown()
        a = self.assertConserved(pool)
        # t2 never ran; the other 5 (t0,t1,t3,t4,t99) did
        with tasks[2].ran_lock:
            self.assertEqual(tasks[2].ran, [])
        self.assertEqual(a["completed"], 5)
        self.assertEqual(a["rejected"], 1)
        self.assertEqual(a["reject_by_reason"]["discarded_oldest"], 1)


class TestShutdownPhases(PoolTestBase):
    def test_submit_during_graceful_shutdown_rejected_then_drain(self):
        pool = ThreadPool(workers=1, capacity=2, policy=Policy.ABORT)
        running = TaskLatch(0, "running")
        queued = TaskLatch(1, "queued")
        pool.submit(running)
        pool.submit(queued)
        running.started.wait(2)

        # phase 1: RUNNING -> SHUTDOWN (graceful): new submits refused,
        # already-queued task still runs
        pool.shutdown(wait=False)
        self.assertEqual(pool.state.value, "shutdown")
        with self.assertRaises(RejectedExecutionError) as ctx:
            pool.submit(lambda: None)
        self.assertIs(ctx.exception.reason, RejectReason.SHUTTING_DOWN)
        self.assertConserved(pool)
        self.assertFalse(queued.started.is_set())  # queued task not yet run

        running.release.set()
        queued.release.set()
        self.assertTrue(pool.wait_terminated(5))

        # phase 2: TERMINATED
        with self.assertRaises(RejectedExecutionError) as ctx:
            pool.submit(lambda: None)
        self.assertIs(ctx.exception.reason, RejectReason.TERMINATED)
        a = self.assertConserved(pool)
        self.assertEqual(a["submitted"], 4)   # 2 accepted + 2 rejected submits
        self.assertEqual(a["completed"], 2)
        self.assertEqual(a["rejected"], 2)
        self.assertEqual(a["reject_by_reason"]["shutting_down"], 1)
        self.assertEqual(a["reject_by_reason"]["terminated"], 1)
        self.assertEqual(a["queued"], 0)
        self.assertEqual(a["running"], 0)

    def test_discard_policy_returns_result_in_shutdown_phases(self):
        pool = ThreadPool(workers=1, capacity=2, policy=Policy.DISCARD)
        gate = TaskLatch(0)
        pool.submit(gate)
        pool.shutdown(wait=False)
        r = pool.submit(lambda: None)
        self.assertFalse(r.accepted)
        self.assertIs(r.reason, RejectReason.SHUTTING_DOWN)
        gate.release.set()
        pool.wait_terminated(5)
        r = pool.submit(lambda: None)
        self.assertFalse(r.accepted)
        self.assertIs(r.reason, RejectReason.TERMINATED)
        self.assertConserved(pool)

    def test_shutdown_now_cancels_queued_with_reason(self):
        pool = ThreadPool(workers=1, capacity=3, policy=Policy.ABORT)
        running = TaskLatch(0, "running")
        q1, q2 = TaskLatch(1, "q1"), TaskLatch(2, "q2")
        pool.submit(running)
        pool.submit(q1)
        pool.submit(q2)
        running.started.wait(2)

        cancelled = pool.shutdown_now()
        self.assertEqual(cancelled, [2, 3])
        # submits while STOPPING rejected explicitly
        with self.assertRaises(RejectedExecutionError) as ctx:
            pool.submit(lambda: None)
        self.assertIs(ctx.exception.reason, RejectReason.STOPPING)
        running.release.set()
        pool.wait_terminated(5)
        with self.assertRaises(RejectedExecutionError) as ctx:
            pool.submit(lambda: None)
        self.assertIs(ctx.exception.reason, RejectReason.TERMINATED)

        a = self.assertConserved(pool)
        self.assertEqual(a["submitted"], 5)   # 3 accepted + 2 rejected submits
        self.assertEqual(a["completed"], 1)
        self.assertEqual(a["rejected"], 4)
        self.assertEqual(a["reject_by_reason"]["shutdown_drained"], 2)
        self.assertEqual(a["reject_by_reason"]["stopping"], 1)
        self.assertEqual(a["reject_by_reason"]["terminated"], 1)
        with q1.ran_lock:
            self.assertEqual(q1.ran, [])
        with q2.ran_lock:
            self.assertEqual(q2.ran, [])

    def test_on_rejected_callback_fires_for_every_rejection(self):
        events = []
        lock = threading.Lock()
        pool = ThreadPool(
            workers=1, capacity=1, policy=Policy.ABORT,
            on_rejected=lambda rec: events.append(rec),
        )
        gate = TaskLatch(0)
        queued = TaskLatch(1)
        pool.submit(gate)
        gate.started.wait(2)
        pool.submit(queued)  # occupy the queue (capacity=1)
        with self.assertRaises(RejectedExecutionError):
            pool.submit(lambda: None)
        gate.release.set()
        queued.release.set()
        pool.shutdown()
        with self.assertRaises(RejectedExecutionError):
            pool.submit(lambda: None)
        self.assertEqual([e.reason for e in events],
                         [RejectReason.QUEUE_FULL, RejectReason.TERMINATED])
        self.assertConserved(pool)


class TestConservationUnderLoad(unittest.TestCase):
    def test_ledger_balanced_with_abort_policy(self):
        n_submitters, per_thread = 8, 500
        pool = ThreadPool(workers=4, capacity=50, policy=Policy.ABORT)
        barrier = threading.Event()

        def producer():
            barrier.wait()
            for _ in range(per_thread):
                try:
                    pool.submit(time.sleep, 0.0005)
                except RejectedExecutionError:
                    pass

        threads = [threading.Thread(target=producer) for _ in range(n_submitters)]
        for t in threads:
            t.start()
        barrier.set()
        for t in threads:
            t.join()
        pool.shutdown()

        a = pool.accounting()
        self.assertTrue(a["conserved"], a)
        self.assertEqual(a["submitted"], n_submitters * per_thread)
        self.assertEqual(
            a["submitted"], a["completed"] + a["rejected"]
        )  # nothing left after termination
        self.assertGreater(a["rejected"], 0)  # capacity 50 must reject something

    def test_ledger_balanced_with_discard_oldest(self):
        pool = ThreadPool(workers=2, capacity=20, policy=Policy.DISCARD_OLDEST)
        for i in range(1000):
            r = pool.submit(time.sleep, 0.0005)
            self.assertTrue(r.accepted)
            self.assertTrue(pool.check_conservation())
        pool.shutdown()
        a = pool.accounting()
        self.assertTrue(a["conserved"], a)
        self.assertEqual(a["submitted"], a["completed"] + a["rejected"])
        self.assertGreater(a["reject_by_reason"].get("discarded_oldest", 0), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

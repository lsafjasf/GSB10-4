"""Self-tests for threadpool: queue-not-full, queue-full (4 policies),
submit-during-shutdown phases, and counter conservation."""

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from threadpool import (Policy, RejectedExecution, RejectReason, State,
                        ThreadPool)


def wait_for(cond, timeout=5.0, interval=0.001):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


class TestQueueNotFull(unittest.TestCase):
    def test_tasks_accepted_and_results_returned(self):
        with ThreadPool(workers=2, capacity=4, policy=Policy.ABORT) as pool:
            results = [pool.submit(lambda x=i: x * 2) for i in range(3)]
            self.assertTrue(all(r.accepted for r in results))
            self.assertEqual([r.future.result(5) for r in results], [0, 2, 4])
            stats = pool.stats()
            self.assertEqual(stats.submitted, 3)
            self.assertTrue(stats.conserved)
        final = pool.stats()
        self.assertEqual(final.completed, 3)
        self.assertEqual(final.rejected, 0)
        self.assertEqual(final.queued, 0)
        self.assertTrue(final.conserved)

    def test_task_exception_propagates_and_counts_as_completed(self):
        def boom():
            raise ValueError("nope")

        with ThreadPool(workers=1, capacity=2) as pool:
            r = pool.submit(boom)
            self.assertTrue(r.accepted)
            with self.assertRaises(ValueError):
                r.future.result(5)
        stats = pool.stats()
        self.assertEqual(stats.completed, 1)
        self.assertTrue(stats.conserved)


class TestQueueFull(unittest.TestCase):
    def _blocked_pool(self, policy):
        """1 worker, capacity 1. Worker is blocked -> queue fills and stays full."""
        pool = ThreadPool(workers=1, capacity=1, policy=policy)
        release = threading.Event()
        pool.submit(lambda: release.wait(5))
        self.assertTrue(wait_for(lambda: pool.stats().running == 1))
        r2 = pool.submit(lambda: "queued")
        self.assertTrue(r2.accepted)
        self.assertTrue(wait_for(lambda: pool.stats().queued == 1))
        return pool, release

    def test_abort_rejects_with_reason(self):
        pool, release = self._blocked_pool(Policy.ABORT)
        r = pool.submit(lambda: "dropped?")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, RejectReason.QUEUE_FULL)
        self.assertEqual(r.policy, Policy.ABORT)
        self.assertIsNone(r.future)
        self.assertEqual(pool.stats().rejected, 1)
        release.set()
        pool.shutdown()
        self.assertTrue(pool.stats().conserved)

    def test_discard_rejects_with_reason(self):
        pool, release = self._blocked_pool(Policy.DISCARD)
        r = pool.submit(lambda: "dropped?")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, RejectReason.QUEUE_FULL)
        self.assertEqual(r.policy, Policy.DISCARD)
        release.set()
        pool.shutdown()
        self.assertTrue(pool.stats().conserved)

    def test_caller_runs_executes_in_caller_thread(self):
        pool, release = self._blocked_pool(Policy.CALLER_RUNS)
        caller_ident = threading.get_ident()
        r = pool.submit(lambda: (threading.get_ident(), 42))
        self.assertTrue(r.accepted)
        self.assertTrue(r.executed_by_caller)
        ran_ident, value = r.future.result(5)
        self.assertEqual(ran_ident, caller_ident)   # ran on the calling thread
        self.assertEqual(value, 42)
        stats = pool.stats()
        self.assertEqual(stats.caller_runs, 1)
        self.assertEqual(stats.rejected, 0)          # caller-run counts as completed
        release.set()
        pool.shutdown()
        final = pool.stats()
        self.assertEqual(final.completed, 3)         # blocker + queued + caller-run
        self.assertTrue(final.conserved)

    def test_discard_oldest_evicts_and_notifies_via_future(self):
        pool, release = self._blocked_pool(Policy.DISCARD_OLDEST)
        # queue holds r2 ("queued"); submitting r3 must evict it
        r3 = pool.submit(lambda: "new-task")
        self.assertTrue(r3.accepted)
        self.assertEqual(pool.stats().rejected, 1)
        self.assertEqual(pool.stats().queued, 1)     # capacity preserved
        release.set()
        pool.shutdown()
        # the evicted task's caller finds out via its Future
        # (r2 future was created inside _blocked_pool; re-derive from stats)
        final = pool.stats()
        self.assertEqual(final.completed, 2)         # blocker + new-task
        self.assertEqual(final.rejected, 1)          # evicted oldest
        self.assertTrue(final.conserved)

    def test_discard_oldest_evicted_future_raises(self):
        pool = ThreadPool(workers=1, capacity=1, policy=Policy.DISCARD_OLDEST)
        release = threading.Event()
        pool.submit(lambda: release.wait(5))
        self.assertTrue(wait_for(lambda: pool.stats().running == 1))
        victim = pool.submit(lambda: "i-am-old")
        pool.submit(lambda: "i-am-new")              # evicts victim
        with self.assertRaises(RejectedExecution) as ctx:
            victim.future.result(5)
        self.assertEqual(ctx.exception.reason, RejectReason.DISCARDED_BY_POLICY)
        release.set()
        pool.shutdown()
        self.assertTrue(pool.stats().conserved)


class TestShutdownPhases(unittest.TestCase):
    def test_submit_while_shutting_down_rejected(self):
        pool = ThreadPool(workers=1, capacity=4, policy=Policy.ABORT)
        release = threading.Event()
        pool.submit(lambda: release.wait(5))
        pool.submit(lambda: "queued")
        self.assertTrue(wait_for(lambda: pool.stats().running == 1))
        pool.shutdown(wait=False)
        self.assertEqual(pool.state, State.SHUTTING_DOWN)
        r = pool.submit(lambda: "too-late")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, RejectReason.SHUTTING_DOWN)
        release.set()
        pool.shutdown(wait=True)
        self.assertEqual(pool.state, State.TERMINATED)
        stats = pool.stats()
        self.assertEqual(stats.completed, 2)         # queued work still drained
        self.assertEqual(stats.rejected, 1)
        self.assertTrue(stats.conserved)

    def test_submit_after_stop_rejected(self):
        pool = ThreadPool(workers=1, capacity=4)
        release = threading.Event()
        pool.submit(lambda: release.wait(5))
        self.assertTrue(wait_for(lambda: pool.stats().running == 1))
        pool.shutdown_now(wait=False)                # -> STOP while task still runs
        self.assertEqual(pool.state, State.STOP)
        r = pool.submit(lambda: "too-late")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, RejectReason.SHUTDOWN)
        release.set()
        pool.shutdown(wait=True)

    def test_submit_after_terminate_rejected(self):
        pool = ThreadPool(workers=2, capacity=4)
        pool.shutdown(wait=True)
        self.assertEqual(pool.state, State.TERMINATED)
        r = pool.submit(lambda: "too-late")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, RejectReason.TERMINATED)
        stats = pool.stats()
        self.assertEqual(stats.submitted, 1)
        self.assertEqual(stats.rejected, 1)
        self.assertTrue(stats.conserved)

    def test_shutdown_now_rejects_queued_tasks_with_notification(self):
        pool = ThreadPool(workers=1, capacity=8)
        release = threading.Event()
        pool.submit(lambda: release.wait(5))
        queued = [pool.submit(lambda i=i: i) for i in range(3)]
        self.assertTrue(wait_for(lambda: pool.stats().queued == 3))
        drained = pool.shutdown_now(wait=False)
        self.assertEqual(drained, 3)
        for r in queued:
            with self.assertRaises(RejectedExecution) as ctx:
                r.future.result(5)
            self.assertEqual(ctx.exception.reason, RejectReason.SHUTDOWN)
        release.set()
        pool.shutdown(wait=True)
        stats = pool.stats()
        self.assertEqual(stats.submitted, 4)
        self.assertEqual(stats.rejected, 3)
        self.assertEqual(stats.completed, 1)
        self.assertTrue(stats.conserved)


class TestConservation(unittest.TestCase):
    def test_counters_balance_under_mixed_load(self):
        pool = ThreadPool(workers=3, capacity=5, policy=Policy.ABORT)
        outcomes = [pool.submit(lambda: time.sleep(0.002)) for _ in range(200)]
        pool.shutdown(wait=True)
        stats = pool.stats()
        accepted = sum(1 for o in outcomes if o.accepted)
        rejected = sum(1 for o in outcomes if not o.accepted)
        self.assertEqual(accepted + rejected, 200)
        self.assertEqual(stats.submitted, 200)
        self.assertEqual(stats.completed, accepted)
        self.assertEqual(stats.rejected, rejected)
        self.assertEqual(stats.queued, 0)
        self.assertEqual(stats.running, 0)
        # submitted == completed + rejected + running + queued
        self.assertTrue(stats.conserved)
        self.assertGreater(rejected, 0)              # the queue really did fill up


if __name__ == "__main__":
    unittest.main(verbosity=2)

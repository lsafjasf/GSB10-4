"""Self tests for delayq - run: python -m unittest discover -s tests -v

Time is always injected; no wall clock is read anywhere in the queue.
"""

import os
import shutil
import tempfile
import unittest

from delayq import (
    DelayedQueue,
    CancelResult,
    NOT_FOUND,
    CANCELLED,
    ALREADY_DELIVERED,
)
from delayq.store import _encode


class QueueTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="delayq-test-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def reopen(self, **kw):
        return DelayedQueue(self.dir, **kw)

    def delivered_ids(self, q, now):
        got = []
        q.pump(now, lambda rec: got.append((rec.id, rec.seq, rec.due)))
        return got


class DeliveryOrderTests(QueueTestBase):
    def test_immediate_delivery_due_zero(self):
        with self.reopen() as q:
            q.schedule("a", 0, "x")
            q.schedule("b", 0, "y")
            got = self.delivered_ids(q, 0)
        # Same due time: enqueue seq wins.
        self.assertEqual([g[0] for g in got], ["a", "b"])

    def test_immediately_due_at_schedule_time(self):
        with self.reopen() as q:
            q.schedule("a", 100)
            self.assertEqual(self.delivered_ids(q, 99), [])
            self.assertEqual([g[0] for g in self.delivered_ids(q, 100)], ["a"])

    def test_future_message_not_due(self):
        with self.reopen() as q:
            q.schedule("far", 10**12)
            self.assertEqual(self.delivered_ids(q, 10**12 - 1), [])
            self.assertEqual(q.pending_count(), 1)
            self.assertEqual([g[0] for g in self.delivered_ids(q, 10**12)],
                             ["far"])

    def test_ties_follow_enqueue_seq_even_with_interleaving(self):
        with self.reopen() as q:
            q.schedule("t1", 50)
            q.schedule("t2", 10)
            q.schedule("t3", 50)
            q.schedule("t4", 10)
            q.schedule("t5", 50)
            got = self.delivered_ids(q, 50)
        self.assertEqual([g[0] for g in got],
                         ["t2", "t4", "t1", "t3", "t5"])

    def test_pump_drains_only_due_prefix(self):
        with self.reopen() as q:
            q.schedule("a", 1)
            q.schedule("b", 2)
            q.schedule("c", 3)
            self.assertEqual([g[0] for g in self.delivered_ids(q, 2)],
                             ["a", "b"])
            self.assertEqual([g[0] for g in self.delivered_ids(q, 3)], ["c"])

    def test_pump_empty_is_noop(self):
        with self.reopen() as q:
            self.assertEqual(q.pump(0, lambda r: None), [])


class CancelTests(QueueTestBase):
    def test_cancel_pending(self):
        with self.reopen() as q:
            seq = q.schedule("a", 100)
            result = q.cancel("a")
            self.assertEqual(result, CancelResult(CANCELLED, seq))
            self.assertTrue(result)
            self.assertEqual(self.delivered_ids(q, 1000), [])

    def test_cancel_unknown_is_not_found(self):
        with self.reopen() as q:
            self.assertEqual(q.cancel("ghost"), CancelResult(NOT_FOUND))

    def test_cancel_delivered_is_already_delivered(self):
        with self.reopen() as q:
            q.schedule("a", 10)
            self.delivered_ids(q, 10)
            self.assertEqual(q.cancel("a"),
                             CancelResult(ALREADY_DELIVERED))

    def test_cancel_delivered_after_restart_still_detected(self):
        with self.reopen() as q:
            q.schedule("a", 10)
            self.delivered_ids(q, 10)
        # No snapshot: history stays in the WAL, so the result is stable
        # across a process restart.
        q = self.reopen()
        try:
            self.assertEqual(q.cancel("a"),
                             CancelResult(ALREADY_DELIVERED))
            self.assertEqual(q.cancel("b"), CancelResult(NOT_FOUND))
        finally:
            q.close()

    def test_cancel_after_snapshot_is_not_found_boundary(self):
        # Documented retention boundary: compaction forgets delivered ids.
        with self.reopen() as q:
            q.schedule("a", 10)
            self.delivered_ids(q, 10)
            q.snapshot()
            self.assertEqual(q.cancel("a"), CancelResult(NOT_FOUND))

    def test_cancel_then_deliver_does_not_deliver(self):
        with self.reopen() as q:
            q.schedule("a", 10)
            q.schedule("b", 10)
            q.cancel("a")
            got = self.delivered_ids(q, 10)
        self.assertEqual([g[0] for g in got], ["b"])

    def test_duplicate_pending_id_rejected(self):
        with self.reopen() as q:
            q.schedule("dup", 10)
            with self.assertRaises(ValueError):
                q.schedule("dup", 20)


class RecoveryTests(QueueTestBase):
    def test_pending_survives_restart_with_same_order(self):
        with self.reopen() as q:
            q.schedule("a", 50)
            q.schedule("b", 10)
            q.schedule("c", 50)
        q = self.reopen()
        try:
            got = self.delivered_ids(q, 50)
            self.assertEqual([g[0] for g in got], ["b", "a", "c"])
        finally:
            q.close()

    def test_restart_between_pumps_preserves_state(self):
        with self.reopen() as q:
            q.schedule("a", 10)
            q.schedule("b", 20)
            self.delivered_ids(q, 10)
        q = self.reopen()
        try:
            self.assertEqual(self.delivered_ids(q, 15), [])
            self.assertEqual([g[0] for g in self.delivered_ids(q, 20)], ["b"])
        finally:
            q.close()

    def test_seq_monotonic_across_restart(self):
        with self.reopen() as q:
            s1 = q.schedule("a", 100)
            q.schedule("b", 100)
            q.cancel("b")
        q = self.reopen()
        try:
            s3 = q.schedule("c", 100)
            self.assertGreater(s3, s1 + 1)
            got = self.delivered_ids(q, 100)
            self.assertEqual([g[1] for g in got], [s1, s3])
        finally:
            q.close()

    def test_cancel_survives_restart(self):
        with self.reopen() as q:
            q.schedule("a", 10)
            q.schedule("b", 10)
            q.cancel("a")
        q = self.reopen()
        try:
            self.assertEqual([g[0] for g in self.delivered_ids(q, 10)], ["b"])
        finally:
            q.close()

    def test_snapshot_keeps_only_pending(self):
        with self.reopen() as q:
            q.schedule("a", 10)
            q.schedule("b", 20)
            q.schedule("c", 30)
            self.delivered_ids(q, 10)
            q.snapshot()
            q.schedule("d", 25)
            self.delivered_ids(q, 20)
        q = self.reopen()
        try:
            got = self.delivered_ids(q, 100)
            self.assertEqual([g[0] for g in got], ["d", "c"])
        finally:
            q.close()

    def test_snapshot_then_restart_cancel_and_schedule(self):
        with self.reopen() as q:
            q.schedule("a", 10)
            q.schedule("b", 20)
            q.snapshot()
            q.cancel("a")
        q = self.reopen()
        try:
            self.assertEqual([g[0] for g in self.delivered_ids(q, 100)], ["b"])
        finally:
            q.close()

    def test_torn_final_wal_line_is_ignored(self):
        # Simulate a crash mid-append: last line has no trailing newline.
        path = os.path.join(self.dir, "wal.log")
        with self.reopen() as q:
            q.schedule("a", 10)
        # Partial append: no trailing newline -> never committed.
        with open(path, "ab") as fh:
            fh.write(_encode({"ty": "SCHED", "seq": 99, "id": "z",
                              "due": 10, "payload": None}).rstrip(b"\n"))
            fh.flush()
        q = self.reopen()
        try:
            got = [g[0] for g in self.delivered_ids(q, 10)]
            self.assertEqual(got, ["a"])
            # Queue is still writable; new seq assignment skips the torn one
            # conservatively (seq 99 is unknown -> next_seq 0, which is fine
            # because the torn record never committed).
            new_seq = q.schedule("b", 10)
            self.assertEqual(new_seq, 1)
        finally:
            q.close()


class SinkFailureTests(QueueTestBase):
    def test_message_marked_delivered_before_sink(self):
        calls = []

        def sink(rec):
            calls.append(rec.id)
            if rec.id == "boom":
                raise RuntimeError("sink failed")

        with self.reopen() as q:
            q.schedule("x", 10)
            q.schedule("boom", 10)
            q.schedule("y", 10)
            with self.assertRaises(RuntimeError):
                q.pump(10, sink)
            self.assertEqual(calls, ["x", "boom"])
            # Resume with a healthy sink: "boom" is not redelivered (the
            # DELIVER record was fsynced first); "y" still goes out.
            q.pump(10, lambda r: calls.append(r.id))
        self.assertEqual(calls, ["x", "boom", "y"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

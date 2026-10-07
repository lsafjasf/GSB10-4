"""Self tests for the dead-letter engine. Time is injected via FakeClock."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlq import (
    FailureClass,
    FailureOutcome,
    FailureReason,
    Processor,
    RetryPolicy,
    Status,
)

POLICY = RetryPolicy(max_attempts=5, base_delay=1.0, multiplier=2.0,
                     max_delay=60.0, jitter_ratio=0.2)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_processor(seed=42, policy=None):
    return Processor(FakeClock(), random.Random(seed), policy or POLICY)


def fail(processor, mid, code, detail=""):
    return processor.on_failure(mid, FailureReason(code, detail, processor._clock()))


class ClassifierTests(unittest.TestCase):
    def test_retryable_codes(self):
        from dlq import classify
        for code in ("NETWORK_TIMEOUT", "HTTP_429", "HTTP_503", "THROTTLED"):
            self.assertIs(classify(FailureReason(code)), FailureClass.RETRYABLE)

    def test_non_retryable_codes(self):
        from dlq import classify
        for code in ("VALIDATION_ERROR", "HTTP_404", "PERMISSION_DENIED"):
            self.assertIs(classify(FailureReason(code)), FailureClass.NON_RETRYABLE)

    def test_unknown_code_fails_closed(self):
        from dlq import classify
        self.assertIs(classify(FailureReason("WEIRD_BRAND_NEW_ERROR")),
                      FailureClass.NON_RETRYABLE)


class DeliveryTests(unittest.TestCase):
    def test_first_failure_schedules_retry(self):
        p = make_processor()
        p.submit("m1", "orders", b'{"x":1}')
        attempt = p.begin_delivery("m1")
        self.assertEqual(attempt, 1)
        outcome = fail(p, "m1", "NETWORK_TIMEOUT")
        self.assertIs(outcome, FailureOutcome.RETRY_SCHEDULED)
        rec = p.record("m1")
        self.assertIs(rec.status, Status.RETRYING)
        self.assertEqual(len(rec.failures), 1)
        # jittered delay is within +/-20% of base delay 1.0
        self.assertAlmostEqual(rec.next_delivery_at - 1000.0,
                               rec.next_delivery_at - 1000.0)
        self.assertTrue(0.8 <= rec.next_delivery_at - 1000.0 <= 1.2)
        self.assertEqual(p.due(), [])

    def test_retry_then_success(self):
        p = make_processor()
        p.submit("m2", "orders", b"hi")
        p.begin_delivery("m2")
        fail(p, "m2", "HTTP_503")
        first_at = p.record("m2").next_delivery_at
        p._clock.advance(first_at - 1000.0)
        self.assertEqual(p.due(), ["m2"])
        attempt = p.begin_delivery("m2")
        self.assertEqual(attempt, 2)
        p.on_success("m2")
        self.assertIs(p.record("m2").status, Status.SUCCEEDED)
        self.assertNotIn("m2", p.dead_letters)

    def test_retry_not_due_until_scheduled_time(self):
        p = make_processor()
        p.submit("m", "q", b"")
        p.begin_delivery("m")
        fail(p, "m", "THROTTLED")
        nxt = p.record("m").next_delivery_at
        self.assertEqual(p.due(nxt - 0.001), [])
        self.assertEqual(p.due(nxt), ["m"])

    def test_attempts_exhausted_go_to_dlq(self):
        p = make_processor()
        p.submit("m3", "orders", b'{"id":3}', {"trace": "abc"})
        for i in range(1, POLICY.max_attempts + 1):
            p.begin_delivery("m3")
            outcome = fail(p, "m3", "HTTP_500", f"boom {i}")
        self.assertIs(outcome, FailureOutcome.DEAD_LETTERED)
        rec = p.record("m3")
        self.assertIs(rec.status, Status.DEAD)
        self.assertEqual(rec.attempts, POLICY.max_attempts)
        entry = p.dead_letter("m3")
        # original message preserved
        self.assertEqual(entry.message_id, "m3")
        self.assertEqual(entry.queue, "orders")
        self.assertEqual(entry.payload, b'{"id":3}')
        self.assertEqual(entry.headers, {"trace": "abc"})
        # full failure history preserved
        self.assertEqual(len(entry.failures), POLICY.max_attempts)
        self.assertEqual([f.attempt for f in entry.failures], [1, 2, 3, 4, 5])
        self.assertTrue(all(f.klass is FailureClass.RETRYABLE for f in entry.failures))
        self.assertEqual(entry.failures[0].reason.detail, "boom 1")

    def test_non_retryable_goes_to_dlq_immediately(self):
        p = make_processor()
        p.submit("m4", "orders", b"bad")
        p.begin_delivery("m4")
        outcome = fail(p, "m4", "VALIDATION_ERROR")
        self.assertIs(outcome, FailureOutcome.DEAD_LETTERED)
        self.assertIs(p.record("m4").status, Status.DEAD)
        self.assertEqual(p.record("m4").attempts, 1)
        self.assertIsNone(p.record("m4").next_delivery_at)
        entry = p.dead_letter("m4")
        self.assertEqual(len(entry.failures), 1)
        self.assertIs(entry.failures[0].klass, FailureClass.NON_RETRYABLE)


class DedupeTests(unittest.TestCase):
    def test_duplicate_failure_report_is_idempotent(self):
        p = make_processor()
        p.submit("d", "q", b"")
        p.begin_delivery("d")
        o1 = fail(p, "d", "HTTP_502")
        o2 = fail(p, "d", "HTTP_502")
        self.assertIs(o1, FailureOutcome.RETRY_SCHEDULED)
        self.assertIs(o2, FailureOutcome.DUPLICATE_IGNORED)
        rec = p.record("d")
        self.assertEqual(len(rec.failures), 1)
        self.assertEqual(rec.attempts, 1)

    def test_duplicate_delivery_does_not_inflate_attempts(self):
        p = make_processor()
        p.submit("d2", "q", b"")
        self.assertEqual(p.begin_delivery("d2"), 1)
        self.assertEqual(p.begin_delivery("d2"), 1)  # at-least-once duplicate
        self.assertEqual(p.record("d2").attempts, 1)
        fail(p, "d2", "NETWORK_TIMEOUT")
        p._clock.advance(100)
        self.assertEqual(p.begin_delivery("d2"), 2)

    def test_submit_same_id_dedupes(self):
        p = make_processor()
        p.submit("d3", "q", b"first")
        p.submit("d3", "other", b"second")
        self.assertEqual(p.record("d3").payload, b"first")

    def test_duplicate_success_is_noop(self):
        p = make_processor()
        p.submit("d4", "q", b"")
        p.begin_delivery("d4")
        p.on_success("d4")
        p.on_success("d4")
        self.assertIs(p.record("d4").status, Status.SUCCEEDED)


class JitterTests(unittest.TestCase):
    def test_schedule_is_reproducible_from_seed(self):
        def schedule(seed):
            proc = make_processor(seed)
            proc.submit("j", "q", b"")
            times = []
            for _ in range(POLICY.max_attempts - 1):
                proc.begin_delivery("j")
                fail(proc, "j", "HTTP_500")
                times.append(proc.record("j").next_delivery_at)
                proc._clock.t = proc.record("j").next_delivery_at
            return times

        self.assertEqual(schedule(42), schedule(42))
        self.assertNotEqual(schedule(42), schedule(7))

    def test_jitter_within_bounds(self):
        p = make_processor()
        p.submit("j2", "q", b"")
        for attempt in range(1, POLICY.max_attempts):
            p.begin_delivery("j2")
            fail(p, "j2", "HTTP_500")
            delay = p.record("j2").next_delivery_at - p._clock.t
            base = POLICY.base_for(attempt)
            self.assertTrue(base * 0.8 - 1e-9 <= delay <= base * 1.2 + 1e-9,
                            (attempt, delay, base))
            p._clock.t = p.record("j2").next_delivery_at


class ReplayTests(unittest.TestCase):
    def _dead_message(self):
        p = make_processor()
        p.submit("r1", "billing", b'{"amt":10}', {"k": "v"})
        p.begin_delivery("r1")
        fail(p, "r1", "PERMISSION_DENIED", "nope")
        return p

    def test_replay_restores_original_message_to_original_queue(self):
        p = self._dead_message()
        entry = p.dead_letter("r1")
        rec = p.replay("r1")
        # --- 对拍 (differential check): requeued delivery vs original ---
        p.begin_delivery("r1")
        self.assertEqual(rec.message_id, entry.message_id)
        self.assertEqual(rec.queue, entry.queue, "must go back to original queue")
        self.assertEqual(rec.payload, entry.payload)
        self.assertEqual(rec.headers, entry.headers)
        self.assertEqual(rec.enqueued_at, entry.enqueued_at)
        # fresh lifecycle
        self.assertEqual(rec.attempts, 1)
        self.assertEqual(rec.replay_count, 1)
        self.assertEqual(entry.replayed, 1)
        # DLQ history is immutable and retained
        self.assertEqual(len(entry.failures), 1)
        self.assertIs(entry.failures[0].klass, FailureClass.NON_RETRYABLE)
        p.on_success("r1")
        self.assertIs(rec.status, Status.SUCCEEDED)

    def test_replay_idempotency_counter_and_repeat_dead_letter(self):
        p = self._dead_message()
        p.replay("r1")
        p.begin_delivery("r1")
        fail(p, "r1", "VALIDATION_ERROR")
        self.assertIs(p.record("r1").status, Status.DEAD)
        self.assertEqual(p.dead_letter("r1").replayed, 1)
        p.replay("r1")
        self.assertEqual(p.record("r1").replay_count, 2)


class CountBoundTests(unittest.TestCase):
    def test_failure_record_count_bounded(self, n=200):
        p = make_processor()
        for i in range(n):
            mid = f"b{i}"
            p.submit(mid, "q", b"x")
            # noisy transport: duplicates the delivery and failure report 3x.
            for _ in range(3):
                p.begin_delivery(mid)
            for _ in range(3):
                fail(p, mid, "HTTP_500")
            p._clock.advance(100)
        total_failures = sum(len(r.failures) for r in p._records.values())
        self.assertEqual(total_failures, n)  # one record each despite 3x reports
        # drive everything to exhaustion with duplicate reports
        for _ in range(POLICY.max_attempts - 1):
            p._clock.advance(100)
            for i in range(n):
                mid = f"b{i}"
                if p.record(mid).status is Status.RETRYING:
                    for _ in range(2):
                        p.begin_delivery(mid)
                    for _ in range(2):
                        fail(p, mid, "HTTP_500")
        total_failures = sum(len(r.failures) for r in p._records.values())
        self.assertEqual(total_failures, n * POLICY.max_attempts)
        self.assertEqual(len(p.dead_letters), n)
        self.assertLessEqual(
            len(p._applied_failures), n * POLICY.max_attempts
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

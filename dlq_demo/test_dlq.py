"""Self-tests for dead-letter classification and bounded retry.

Run: python -m unittest dlq_demo.test_dlq -v   (from repo root)
"""

import unittest

from .dlq import (
    Category,
    Clock,
    DeadLetterProcessor,
    ErrorCode,
    Message,
    Phase,
    RetryPolicy,
    classify,
)


def _run_until_dead(proc, mid, code=ErrorCode.TIMEOUT, detail="x"):
    """Drive all due attempts of one message, failing each."""
    while True:
        due = proc.due_ids()
        if mid not in due:
            return None
        proc.fail(mid, code, detail)
        if proc.phase(mid) is Phase.DEAD:
            return proc.get_dead(mid)
        proc.clock.set(proc.next_attempt_at(mid))


class ClassificationTests(unittest.TestCase):
    def test_builtin_categories(self):
        retryable = [
            ErrorCode.TIMEOUT, ErrorCode.CONNECTION_RESET, ErrorCode.UNAVAILABLE,
            ErrorCode.RATE_LIMITED, ErrorCode.INTERNAL,
        ]
        non_retryable = [
            ErrorCode.VALIDATION, ErrorCode.DESERIALIZATION, ErrorCode.AUTH,
            ErrorCode.NOT_FOUND, ErrorCode.UNKNOWN,
        ]
        for code in retryable:
            self.assertIs(classify(code), Category.RETRYABLE, code)
        for code in non_retryable:
            self.assertIs(classify(code), Category.NON_RETRYABLE, code)

    def test_unknown_code_fails_safe_non_retryable(self):
        self.assertIs(classify("some-brand-new-error"), Category.NON_RETRYABLE)

    def test_overrides(self):
        policy = RetryPolicy(category_overrides={"poison": Category.RETRYABLE})
        self.assertIs(classify("poison", policy.category_overrides), Category.RETRYABLE)


class RetryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(0.0)
        self.proc = DeadLetterProcessor(RetryPolicy(seed=0), self.clock)
        self.msg = Message(id="m1", queue="orders", payload=b'{"a":1}', headers={"x": "y"})

    def test_first_failure_is_scheduled_not_dead(self):
        self.proc.enqueue(self.msg)
        self.assertEqual(self.proc.due_ids(), ["m1"])  # immediate first attempt
        cat = self.proc.fail("m1", ErrorCode.TIMEOUT)
        self.assertIs(cat, Category.RETRYABLE)
        self.assertEqual(self.proc.attempts("m1"), 1)
        self.assertEqual(self.proc.failure_count("m1"), 1)
        self.assertIs(self.proc.phase("m1"), Phase.PENDING)
        self.assertFalse(self.proc.due_ids())  # nothing due before backoff elapses
        self.assertEqual(self.proc.dead_count(), 0)

    def test_succeeds_after_retry(self):
        self.proc.enqueue(self.msg)
        self.proc.due_ids()
        self.proc.fail("m1", ErrorCode.TIMEOUT)
        # The retry must not be handed out before its scheduled time.
        self.clock.set(0.1)
        self.assertEqual(self.proc.due_ids(), [])
        self.clock.set(0.20325894405109435)
        self.assertEqual(self.proc.due_ids(), ["m1"])
        self.assertTrue(self.proc.ack("m1"))
        self.assertEqual(self.proc.attempts("m1"), 2)
        self.assertEqual(self.proc.failure_count("m1"), 1)
        self.assertEqual(self.proc.dead_count(), 0)
        # no further attempts ever come due
        self.clock.set(1e9)
        self.assertEqual(self.proc.due_ids(), [])

    def test_exhausted_retries_go_dead_with_full_history(self):
        self.proc.enqueue(self.msg)
        dl = _run_until_dead(self.proc, "m1")
        self.assertIsNotNone(dl)
        self.assertEqual(dl.attempts, 5)
        self.assertEqual(self.proc.dead_count(), 1)
        self.assertEqual(len(dl.failures), 5)
        self.assertEqual([f.code for f in dl.failures], [ErrorCode.TIMEOUT] * 5)
        # original message preserved byte-for-byte
        self.assertEqual(dl.message.payload, self.msg.payload)
        self.assertEqual(dl.message.headers, {"x": "y"})
        self.assertEqual(dl.message.queue, "orders")

    def test_non_retryable_goes_dead_immediately(self):
        self.proc.enqueue(self.msg)
        self.proc.due_ids()
        cat = self.proc.fail("m1", ErrorCode.VALIDATION)
        self.assertIs(cat, Category.NON_RETRYABLE)
        self.assertEqual(self.proc.attempts("m1"), 1)
        self.assertEqual(self.proc.dead_count(), 1)
        self.clock.set(1e9)
        self.assertEqual(self.proc.due_ids(), [])  # never retried

    def test_mixed_sequence_does_not_consume_budget_on_poison(self):
        # poison failures get exactly one attempt; transient get up to five
        poison = Message(id="p", queue="q", payload=b"p")
        self.proc.enqueue(self.msg)
        self.proc.enqueue(poison)
        self.proc.due_ids()
        self.proc.fail("m1", ErrorCode.TIMEOUT)
        self.proc.fail("p", ErrorCode.VALIDATION)
        self.assertEqual(self.proc.dead_count(), 1)
        self.assertIsNotNone(self.proc.get_dead("p"))


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(0.0)
        self.proc = DeadLetterProcessor(RetryPolicy(seed=0, max_requeues=1), self.clock)

    def test_duplicate_delivery_rejected(self):
        msg = Message(id="dup", queue="q", payload=b"x")
        self.assertTrue(self.proc.enqueue(msg))
        self.assertFalse(self.proc.enqueue(Message(id="dup", queue="q", payload=b"other")))
        self.proc.due_ids()
        self.proc.fail("dup", ErrorCode.TIMEOUT)
        self.assertEqual(self.proc.attempts("dup"), 1)
        self.assertEqual(self.proc.failure_count("dup"), 1)

    def test_duplicate_failure_report_is_idempotent(self):
        self.proc.enqueue(Message(id="d", queue="q", payload=b"x"))
        self.proc.due_ids()
        self.proc.fail("d", ErrorCode.TIMEOUT)
        # duplicate nack of the just-failed attempt: no counter movement
        self.proc.fail("d", ErrorCode.TIMEOUT)
        self.assertEqual(self.proc.attempts("d"), 1)
        self.assertEqual(self.proc.failure_count("d"), 1)
        # next attempt: reporting the same outcome three times is still one
        # record; changing the outcome code of a finished attempt is rejected
        self.clock.set(self.proc.next_attempt_at("d"))
        self.proc.due_ids()
        self.proc.fail("d", ErrorCode.TIMEOUT)
        self.proc.fail("d", ErrorCode.TIMEOUT)
        self.proc.fail("d", ErrorCode.TIMEOUT)
        self.assertEqual(self.proc.attempts("d"), 2)
        self.assertEqual(self.proc.failure_count("d"), 2)
        with self.assertRaises(KeyError):
            self.proc.fail("d", ErrorCode.UNAVAILABLE)

    def test_counter_upper_bounds_under_repeated_failure_and_dupes(self):
        p = RetryPolicy(seed=1, max_attempts=5, max_requeues=2)
        clock = Clock(0.0)
        proc = DeadLetterProcessor(p, clock)
        proc.enqueue(Message(id="bounded", queue="q", payload=b"x"))
        for cycle in range(p.max_requeues + 2):  # exceed the requeue cap
            while proc.phase("bounded") is not Phase.DEAD:
                due = proc.due_ids()
                if not due:
                    clock.set(proc.next_attempt_at("bounded"))
                    continue
                mid = due[0]
                # each attempt reports the same failure three times
                proc.fail(mid, ErrorCode.TIMEOUT)
                proc.fail(mid, ErrorCode.TIMEOUT)
                proc.fail(mid, ErrorCode.TIMEOUT)
                if proc.phase("bounded") is Phase.PENDING:
                    clock.set(proc.next_attempt_at("bounded"))
            dl = proc.get_dead("bounded")
            if dl is not None and proc.requeue("bounded"):
                continue
            break
        dl = proc.get_dead("bounded")
        self.assertIsNotNone(dl, "must eventually rest in dead letter")
        # Upper bounds (proof exercised, not just claimed):
        self.assertLessEqual(dl.attempts, p.max_attempts)
        self.assertLessEqual(dl.requeues, p.max_requeues)
        self.assertLessEqual(len(dl.failures), p.max_attempts * (p.max_requeues + 1))
        self.assertFalse(proc.requeue("bounded"))  # cap enforced


class ScheduleTests(unittest.TestCase):
    def test_backoff_schedule_is_reproducible_and_order_independent(self):
        expected = [
            0.20325894405109435,  # retry 1: U(0,1)
            0.20325894405109435 + 0.24805652250638022,  # U(0,2)
            0.20325894405109435 + 0.24805652250638022 + 1.332620314827032,  # U(0,4)
            0.20325894405109435 + 0.24805652250638022 + 1.332620314827032 + 1.4332737729552987,
        ]
        runs = []
        for extra in ([], [Message(id="other", queue="q", payload=b"o")]):
            clock = Clock(0.0)
            proc = DeadLetterProcessor(RetryPolicy(seed=0), clock)
            proc.enqueue(Message(id="m1", queue="q", payload=b"x"))
            for msg in extra:
                proc.enqueue(msg)
                proc.due_ids()
                proc.fail(msg.id, ErrorCode.TIMEOUT)
            times = []
            for attempt in range(4):
                proc.due_ids()
                proc.fail("m1", ErrorCode.TIMEOUT)
                times.append(proc.next_attempt_at("m1"))
                clock.set(proc.next_attempt_at("m1"))
            runs.append(times)
        self.assertEqual(runs[0], expected)
        self.assertEqual(runs[1], expected)  # independent of interleaved traffic

    def test_no_jitter_is_pure_backoff(self):
        clock = Clock(0.0)
        proc = DeadLetterProcessor(RetryPolicy(jitter=False), clock)
        proc.enqueue(Message(id="z", queue="q", payload=b"x"))
        times = []
        for _ in range(4):
            proc.due_ids()
            proc.fail("z", ErrorCode.TIMEOUT)
            times.append(proc.next_attempt_at("z"))
            clock.set(proc.next_attempt_at("z"))
        self.assertEqual(times, [1.0, 3.0, 7.0, 15.0])


class RequeueTests(unittest.TestCase):
    def test_requeue_roundtrip_preserves_message_and_history(self):
        clock = Clock(0.0)
        proc = DeadLetterProcessor(RetryPolicy(seed=0, max_requeues=1), clock)
        msg = Message(id="r", queue="origin-q", payload=b'{"v":42}', headers={"trace": "t1"})
        proc.enqueue(msg)
        dl = _run_until_dead(proc, "r")
        self.assertEqual(dl.attempts, 5)
        self.assertTrue(proc.requeue("r"))
        self.assertEqual(proc.phase("r").value, "pending")
        self.assertEqual(proc.attempts("r"), 0)
        # history retained across the dead -> live boundary
        self.assertEqual(proc.failure_count("r"), 5)
        due = proc.due_ids()
        self.assertEqual(due, ["r"])
        self.assertTrue(proc.ack("r"))
        dl2 = proc.get_dead("r")
        # original message still byte-identical on the origin queue path
        state = proc._states["r"]
        self.assertEqual(state.message.payload, b'{"v":42}')
        self.assertEqual(state.message.queue, "origin-q")
        self.assertEqual(state.message.headers, {"trace": "t1"})
        self.assertEqual(state.requeues, 1)

    def test_requeue_cap_holds(self):
        clock = Clock(0.0)
        p = RetryPolicy(seed=0, max_requeues=1)
        proc = DeadLetterProcessor(p, clock)
        proc.enqueue(Message(id="r2", queue="q", payload=b"x"))
        _run_until_dead(proc, "r2")
        self.assertTrue(proc.requeue("r2"))
        _run_until_dead(proc, "r2")
        self.assertFalse(proc.requeue("r2"))
        self.assertEqual(proc.dead_count(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Self-tests for txn_retry. Time is injected: no real sleeping happens."""

import random
import unittest

from txn_retry import (
    ConflictKind,
    Retryer,
    RetryPolicy,
    Status,
    TxnConflict,
)


class FakeClock:
    """Deterministic clock; sleep() advances it instantly."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0.0
        self.now += seconds


def scripted(*events):
    """Build a txn body from a script: values to return / conflicts to raise."""
    it = iter(events)

    def txn():
        ev = next(it)
        if isinstance(ev, TxnConflict):
            raise ev
        return ev

    return txn


def make_retryer(policy=None, seed=42):
    clock = FakeClock()
    rng = random.Random(seed)
    retryer = Retryer(
        policy=policy or RetryPolicy(),
        clock=clock.time,
        sleep=clock.sleep,
        rng=rng,
    )
    return retryer, clock


class FirstSuccessTest(unittest.TestCase):
    def test_success_on_first_attempt(self):
        retryer, clock = make_retryer()
        res = retryer.run(scripted("ok"))
        self.assertEqual(res.status, Status.SUCCESS)
        self.assertEqual(res.value, "ok")
        self.assertEqual(res.num_attempts, 1)
        self.assertEqual(res.num_retries, 0)
        self.assertEqual(res.schedule, [])
        self.assertIsNone(res.last_error)
        self.assertEqual(clock.now, 0.0)  # no time was spent backing off


class SingleConflictTest(unittest.TestCase):
    def test_one_deadlock_then_success(self):
        retryer, _ = make_retryer(seed=42)
        res = retryer.run(scripted(TxnConflict(ConflictKind.DEADLOCK), "ok"))
        self.assertEqual(res.status, Status.SUCCESS)
        self.assertEqual(res.num_attempts, 2)
        self.assertEqual(res.num_retries, 1)
        self.assertEqual(len(res.schedule), 1)
        # full jitter: delay in [0, base_delay=0.010)
        self.assertTrue(0.0 <= res.delays[0] < 0.010)
        self.assertAlmostEqual(res.schedule[0], res.delays[0])
        self.assertEqual(res.last_error.kind, ConflictKind.DEADLOCK)

    def test_retry_time_sequence_is_reproducible(self):
        # Same seed -> identical retry schedule across processes.
        expected_schedule = [0.006394267984578837]
        retryer, _ = make_retryer(seed=42)
        res = retryer.run(scripted(TxnConflict(ConflictKind.DEADLOCK), "ok"))
        self.assertEqual(res.schedule, expected_schedule)


class ConsecutiveConflictsTest(unittest.TestCase):
    def test_three_conflicts_then_success(self):
        retryer, clock = make_retryer(seed=7)
        res = retryer.run(
            scripted(
                TxnConflict(ConflictKind.DEADLOCK),
                TxnConflict(ConflictKind.LOCK_WAIT_TIMEOUT),
                TxnConflict(ConflictKind.SERIALIZATION_FAILURE),
                "ok",
            )
        )
        self.assertEqual(res.status, Status.SUCCESS)
        self.assertEqual(res.num_attempts, 4)
        self.assertEqual(res.num_retries, 3)
        # caps: 0.010, 0.020, 0.040  (exponential growth before jitter)
        caps = [0.010, 0.020, 0.040]
        for delay, cap in zip(res.delays, caps):
            self.assertTrue(0.0 <= delay < cap)
        # schedule is cumulative and strictly non-decreasing
        self.assertEqual(res.schedule, sorted(res.schedule))
        self.assertAlmostEqual(res.schedule[-1], sum(res.delays))
        self.assertAlmostEqual(clock.now, sum(res.delays))

    def test_persistent_conflict_reaches_terminal_state(self):
        retryer, _ = make_retryer(seed=1)
        policy = retryer.policy

        def always_deadlock():
            raise TxnConflict(ConflictKind.DEADLOCK, "victim of deadlock #7")

        res = retryer.run(always_deadlock)
        self.assertEqual(res.status, Status.EXHAUSTED_ATTEMPTS)
        self.assertEqual(res.num_attempts, policy.max_attempts)
        self.assertEqual(res.num_retries, policy.max_attempts - 1)
        # terminal state keeps the LAST failure reason
        self.assertIsNotNone(res.last_error)
        self.assertEqual(res.last_error.kind, ConflictKind.DEADLOCK)
        self.assertEqual(str(res.last_error), "victim of deadlock #7")

    def test_mixed_retryable_kinds_all_retried(self):
        retryer, _ = make_retryer(seed=3)
        res = retryer.run(
            scripted(
                TxnConflict(ConflictKind.LOCK_WAIT_TIMEOUT),
                TxnConflict(ConflictKind.SERIALIZATION_FAILURE),
                "ok",
            )
        )
        self.assertEqual(res.status, Status.SUCCESS)
        self.assertEqual(res.num_retries, 2)


class NonRetryableTest(unittest.TestCase):
    def test_constraint_violation_fails_fast(self):
        retryer, clock = make_retryer()
        res = retryer.run(
            scripted(TxnConflict(ConflictKind.CONSTRAINT_VIOLATION, "dup key"))
        )
        self.assertEqual(res.status, Status.NON_RETRYABLE)
        self.assertEqual(res.num_attempts, 1)
        self.assertEqual(res.num_retries, 0)
        self.assertEqual(res.last_error.kind, ConflictKind.CONSTRAINT_VIOLATION)
        self.assertEqual(clock.now, 0.0)  # zero backoff time spent

    def test_fatal_after_retryable_still_fails_fast(self):
        retryer, _ = make_retryer(seed=5)
        res = retryer.run(
            scripted(
                TxnConflict(ConflictKind.DEADLOCK),
                TxnConflict(ConflictKind.PERMISSION_DENIED),
            )
        )
        self.assertEqual(res.status, Status.NON_RETRYABLE)
        self.assertEqual(res.num_attempts, 2)
        self.assertEqual(res.last_error.kind, ConflictKind.PERMISSION_DENIED)


class SharedBudgetTest(unittest.TestCase):
    def test_budget_is_shared_across_retries(self):
        # Budget smaller than the sum of possible delays: retries must stop
        # before total scheduled time exceeds the budget.
        policy = RetryPolicy(base_delay=0.05, max_delay=10.0, max_attempts=100,
                             budget=0.20)
        retryer, clock = make_retryer(policy=policy, seed=11)

        def always_conflict():
            raise TxnConflict(ConflictKind.DEADLOCK)

        res = retryer.run(always_conflict)
        self.assertEqual(res.status, Status.EXHAUSTED_BUDGET)
        self.assertLessEqual(clock.now, policy.budget)
        self.assertLess(res.num_attempts, policy.max_attempts)
        self.assertIsNotNone(res.last_error)
        self.assertEqual(res.last_error.kind, ConflictKind.DEADLOCK)

    def test_total_time_never_exceeds_budget(self):
        for seed in range(50):
            policy = RetryPolicy(base_delay=0.01, max_delay=1.0,
                                 max_attempts=1000, budget=0.5)
            retryer, clock = make_retryer(policy=policy, seed=seed)

            def always_conflict():
                raise TxnConflict(ConflictKind.SERIALIZATION_FAILURE)

            res = retryer.run(always_conflict)
            self.assertLessEqual(clock.now, policy.budget, f"seed={seed}")
            self.assertIn(res.status,
                          (Status.EXHAUSTED_BUDGET, Status.EXHAUSTED_ATTEMPTS))

    def test_budget_consumption_is_reported(self):
        policy = RetryPolicy(base_delay=0.05, max_delay=10.0, max_attempts=100,
                             budget=0.20)
        retryer, _ = make_retryer(policy=policy, seed=11)

        def always_conflict():
            raise TxnConflict(ConflictKind.DEADLOCK)

        res = retryer.run(always_conflict)
        consumed = sum(res.delays)
        self.assertAlmostEqual(consumed, res.elapsed)
        self.assertLessEqual(consumed, policy.budget)
        self.assertGreater(consumed, 0.0)


class ReproducibilityTest(unittest.TestCase):
    def test_same_seed_same_schedule(self):
        def run_once(seed):
            retryer, _ = make_retryer(seed=seed)
            return retryer.run(
                scripted(
                    TxnConflict(ConflictKind.DEADLOCK),
                    TxnConflict(ConflictKind.DEADLOCK),
                    TxnConflict(ConflictKind.DEADLOCK),
                    "ok",
                )
            )

        a = run_once(123)
        b = run_once(123)
        c = run_once(124)
        self.assertEqual(a.schedule, b.schedule)
        self.assertNotEqual(a.schedule, c.schedule)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""txretry 自测：时间全部由 FakeClock 注入，结果完全可复现。

运行：python3 -m unittest -v test_txretry
"""

import random
import unittest

from txretry import (
    AbortError,
    DeadlockError,
    FakeClock,
    FatalTransactionError,
    LockTimeoutError,
    RetryBudgetExhausted,
    RetryClass,
    RetryPolicy,
    RetryState,
    SerializationError,
    classify,
    run_transaction,
)


class _SQLError(Exception):
    """模拟驱动异常（PG 风格：带 sqlstate）。"""

    def __init__(self, sqlstate, message="sql error"):
        super().__init__(message)
        self.sqlstate = sqlstate


class _MySQLError(Exception):
    """模拟 MySQL 驱动：args[0] 为错误码。"""


# ------------------------------------------------------------- 冲突分类规则

class ClassificationTests(unittest.TestCase):
    def test_retryable_library_errors(self):
        for exc in (DeadlockError(), SerializationError(), LockTimeoutError()):
            with self.subTest(exc=type(exc).__name__):
                self.assertIs(classify(exc), RetryClass.RETRYABLE)

    def test_retryable_pg_sqlstate(self):
        for code in ("40P01", "40001", "55P03"):
            with self.subTest(code=code):
                self.assertIs(classify(_SQLError(code)), RetryClass.RETRYABLE)

    def test_retryable_40_class_prefix(self):
        # 任何 40xxx（transaction rollback 整类）都可重试
        self.assertIs(classify(_SQLError("40XXX")), RetryClass.RETRYABLE)

    def test_retryable_mysql_codes(self):
        self.assertIs(classify(_MySQLError(1213)), RetryClass.RETRYABLE)  # 死锁
        self.assertIs(classify(_MySQLError(1205)), RetryClass.RETRYABLE)  # 锁超时

    def test_fatal_constraint_violations(self):
        for code, name in [
            ("23505", "unique_violation"),
            ("23503", "foreign_key_violation"),
            ("23502", "not_null_violation"),
            ("23514", "check_violation"),
        ]:
            with self.subTest(code=code, name=name):
                self.assertIs(classify(_SQLError(code)), RetryClass.FATAL)

    def test_fatal_other_pg_sqlstate(self):
        for code in ("42501", "22012", "42804", "08006", "42601"):
            with self.subTest(code=code):
                self.assertIs(classify(_SQLError(code)), RetryClass.FATAL)

    def test_fatal_mysql_codes(self):
        self.assertIs(classify(_MySQLError(1062)), RetryClass.FATAL)  # 唯一键
        self.assertIs(classify(_MySQLError(1452)), RetryClass.FATAL)  # 外键

    def test_fatal_business_abort(self):
        self.assertIs(classify(AbortError("余额不足")), RetryClass.FATAL)

    def test_unknown_error_is_fatal_fail_closed(self):
        self.assertIs(classify(RuntimeError("unknown")), RetryClass.FATAL)
        self.assertIs(classify(Exception()), RetryClass.FATAL)

    def test_explicit_retryable_attribute_wins(self):
        class WeirdError(Exception):
            retryable = True
        self.assertIs(classify(WeirdError()), RetryClass.RETRYABLE)


# ------------------------------------------------------------------- 场景

class ScenarioTests(unittest.TestCase):
    def make_policy(self, **kw):
        kw.setdefault("max_attempts", 5)
        kw.setdefault("base_delay", 0.01)
        kw.setdefault("max_delay", 0.5)
        kw.setdefault("time_budget", 2.0)
        return RetryPolicy(**kw)

    # 情形 1：首次成功
    def test_first_try_success(self):
        clock = FakeClock()
        calls = []

        def tx():
            calls.append(clock.now())
            return "ok"

        result = run_transaction(tx, policy=self.make_policy(), clock=clock,
                                 rng=random.Random(1))
        self.assertIs(result.state, RetryState.SUCCESS)
        self.assertEqual(result.value, "ok")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.retries, 0)
        self.assertEqual(result.elapsed, 0.0)
        self.assertEqual(result.budget_remaining, 2.0)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(len(calls), 1)

    # 情形 2：单次冲突后成功
    def test_single_conflict_then_success(self):
        clock = FakeClock()
        times = []

        def tx():
            times.append(clock.now())
            if len(times) == 1:
                raise DeadlockError()
            return "committed"

        result = run_transaction(tx, policy=self.make_policy(), clock=clock,
                                 rng=random.Random(42))
        self.assertIs(result.state, RetryState.SUCCESS)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.retries, 1)
        self.assertEqual(result.value, "committed")
        self.assertEqual(len(clock.sleeps), 1)
        self.assertGreater(clock.sleeps[0], 0.0)
        self.assertLessEqual(clock.sleeps[0], 0.01)  # cap = base*2^0
        # 第二次尝试起点 = 退避时长（首次执行耗时为 0）
        self.assertAlmostEqual(times[1], clock.sleeps[0])

    # 情形 3：连续冲突达上限 -> EXHAUSTED，保留最后原因
    def test_continuous_conflict_exhausts_attempts(self):
        clock = FakeClock()
        errors = [DeadlockError(f"victim-{i}") for i in range(5)]
        idx = {"i": 0}

        def tx():
            err = errors[idx["i"]]
            idx["i"] += 1
            raise err

        policy = self.make_policy(max_attempts=4)
        with self.assertRaises(RetryBudgetExhausted) as cm:
            run_transaction(tx, policy=policy, clock=clock,
                            rng=random.Random(7))
        terminal = cm.exception
        self.assertIs(terminal.state, RetryState.EXHAUSTED)
        self.assertEqual(terminal.attempts, 4)
        self.assertIs(terminal.last_error, errors[3])
        self.assertIn("exhausted", str(terminal))
        self.assertIn("victim-3", str(terminal))
        # 最后一次失败后不再睡眠
        self.assertEqual(len(clock.sleeps), 3)
        # 每次退避都不超过对应 cap
        caps = [policy.backoff_cap(i) for i in range(3)]
        for slept, cap in zip(clock.sleeps, caps):
            self.assertGreaterEqual(slept, 0.0)
            self.assertLessEqual(slept, cap)

    # 情形 4：不可重试错误 -> FATAL，立即失败不睡眠
    def test_fatal_error_fails_immediately(self):
        clock = FakeClock()
        calls = {"n": 0}

        def tx():
            calls["n"] += 1
            raise AbortError("余额不足，拒绝透支")

        with self.assertRaises(AbortError):
            run_transaction(tx, policy=self.make_policy(), clock=clock,
                            rng=random.Random(1))
        self.assertEqual(calls["n"], 1)
        self.assertEqual(clock.sleeps, [])

    def test_fatal_after_one_retry_does_not_sleep(self):
        clock = FakeClock()
        state = {"n": 0}

        def tx():
            state["n"] += 1
            if state["n"] == 1:
                raise DeadlockError()
            raise _SQLError("23505", "duplicate key")

        with self.assertRaises(_SQLError) as cm:
            run_transaction(tx, policy=self.make_policy(), clock=clock,
                            rng=random.Random(3))
        self.assertEqual(cm.exception.sqlstate, "23505")
        self.assertEqual(state["n"], 2)
        self.assertEqual(len(clock.sleeps), 1)  # 只在第一次可重试冲突后睡

    # 情形 5：共享时间预算先于次数耗尽 -> BUDGET_EXHAUSTED
    def test_shared_time_budget_exhausted(self):
        clock = FakeClock()

        def tx():
            clock.advance(0.6)  # 每次尝试本身耗时 0.6s
            raise DeadlockError()

        policy = RetryPolicy(max_attempts=100, base_delay=0.01,
                             max_delay=0.5, time_budget=2.0)
        with self.assertRaises(RetryBudgetExhausted) as cm:
            run_transaction(tx, policy=policy, clock=clock,
                            rng=random.Random(9))
        terminal = cm.exception
        self.assertIs(terminal.state, RetryState.BUDGET_EXHAUSTED)
        # 调度侧硬上界：终止时 elapsed <= 预算 + 单次执行耗时(0.6)
        self.assertLessEqual(terminal.elapsed, 2.0 + 0.6 + 1e-9)
        self.assertIsInstance(terminal.last_error, DeadlockError)
        self.assertLess(terminal.attempts, 100)
        # 所有已发生的退避都在预算内被调度
        self.assertLessEqual(sum(clock.sleeps), 2.0 + 1e-9)

    # 预算不够下一觉时直接终止，不越过预算
    def test_delay_exceeding_budget_aborts_without_sleep(self):
        clock = FakeClock(start=0.0)

        def tx():
            clock.advance(1.9)
            raise DeadlockError()

        policy = RetryPolicy(max_attempts=10, base_delay=0.5,
                             max_delay=5.0, time_budget=2.0)
        with self.assertRaises(RetryBudgetExhausted) as cm:
            run_transaction(tx, policy=policy, clock=clock,
                            rng=random.Random(11))
        self.assertIs(cm.exception.state, RetryState.BUDGET_EXHAUSTED)
        self.assertEqual(clock.now(), 1.9)  # 没有睡过头


# ------------------------------------------------------------- 抖动与可复现

class JitterTests(unittest.TestCase):
    def test_full_jitter_reproducible_series(self):
        """相同种子 -> 相同退避序列；不同种子 -> 不同序列（且摊开）。"""
        policy = RetryPolicy(max_attempts=8, base_delay=0.01,
                             max_delay=1.0, jitter="full")

        def series(seed):
            rng = random.Random(seed)
            return [policy.compute_delay(i, rng) for i in range(7)]

        s1, s2 = series(2024), series(2024)
        self.assertEqual(s1, s2)
        s3 = series(2025)
        self.assertNotEqual(s1, s3)
        # full jitter 有明显方差：不全等于 cap
        self.assertTrue(any(d < policy.backoff_cap(i)
                            for i, d in enumerate(s1)))
        self.assertTrue(any(d > 0.0 for d in s1))

    def test_no_jitter_synchronized_cliff(self):
        """对照实验：无抖动时所有客户端在同一时刻撞回来。"""
        policy = RetryPolicy(jitter="none", base_delay=0.01, max_delay=1.0)
        rng = random.Random(0)
        delays = [policy.compute_delay(i, rng) for i in range(4)]
        caps = [policy.backoff_cap(i) for i in range(4)]
        self.assertEqual(delays, caps)
        # 两个独立客户端退避序列完全相同 -> 同步撞回
        rng_b = random.Random(999)
        self.assertEqual(delays, [policy.compute_delay(i, rng_b)
                                  for i in range(4)])

    def test_equal_jitter_bounds(self):
        policy = RetryPolicy(jitter="equal", base_delay=0.01, max_delay=1.0)
        rng = random.Random(5)
        for i in range(5):
            cap = policy.backoff_cap(i)
            d = policy.compute_delay(i, rng)
            self.assertGreaterEqual(d, cap / 2.0)
            self.assertLessEqual(d, cap)

    def test_backoff_cap_grows_then_caps(self):
        p = RetryPolicy(base_delay=0.01, max_delay=0.5)
        self.assertAlmostEqual(p.backoff_cap(0), 0.01)
        self.assertAlmostEqual(p.backoff_cap(1), 0.02)
        self.assertAlmostEqual(p.backoff_cap(2), 0.04)
        self.assertAlmostEqual(p.backoff_cap(6), 0.5)   # 0.64 被截
        self.assertAlmostEqual(p.backoff_cap(9), 0.5)


# ------------------------------------------------------------- on_retry 钩子

class HookTests(unittest.TestCase):
    def test_history_and_hook(self):
        clock = FakeClock()
        events = []

        def tx():
            if clock.now() < 0.5:
                raise DeadlockError()
            return 42

        def hook(record):
            events.append((record.attempt, record.retry_class.value,
                           record.delay_after))

        result = run_transaction(
            tx,
            policy=RetryPolicy(max_attempts=10, base_delay=0.3,
                               max_delay=1.0, time_budget=10.0),
            clock=clock, rng=random.Random(1), on_retry=hook)
        self.assertEqual(result.value, 42)
        self.assertEqual(result.attempts - 1, len(events))
        for h, e in zip(result.history, events):
            self.assertEqual((h.attempt, h.retry_class.value, h.delay_after), e)
        self.assertTrue(all(h.error is not None for h in result.history))


if __name__ == "__main__":
    unittest.main(verbosity=2)

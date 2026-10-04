"""budget_chain 自测：python -m unittest -v"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from budget_chain import FakeClock, Stage, TimeBudget, run_chain  # noqa: E402


def make_stage(
    name,
    plan,
    *,
    weight=1.0,
    max_attempts=1,
    max_attempt_ms=None,
    min_attempt_ms=0.0,
    backoff_base_ms=0.0,
    backoff_factor=2.0,
):
    """plan: 每次尝试的行为列表，元素为毫秒数（成功，sleep 该时长）或 "timeout"。"""

    calls = {"n": 0}

    def handler(ctx, attempt_timeout_ms):
        clock = handler_clock[0]
        action = plan[min(calls["n"], len(plan) - 1)]
        calls["n"] += 1
        if action == "timeout" or action > attempt_timeout_ms:
            clock.sleep(attempt_timeout_ms)
            raise TimeoutError(f"{name} downstream timeout after {attempt_timeout_ms}ms")
        clock.sleep(action)
        return f"{name}:ok#{calls['n']}"

    return Stage(
        name=name,
        handler=handler,
        weight=weight,
        max_attempts=max_attempts,
        max_attempt_ms=max_attempt_ms,
        min_attempt_ms=min_attempt_ms,
        backoff_base_ms=backoff_base_ms,
        backoff_factor=backoff_factor,
    )


handler_clock = {}


def chain(total_ms, stages, *, queue=0.0, start_ms=0.0):
    clock = FakeClock(start_ms)
    handler_clock[0] = clock
    budget = TimeBudget.start(total_ms, clock=clock, sleep=clock.sleep)
    result = run_chain(budget, stages, queue_wait_ms=queue)
    return result, clock


class BudgetChainTests(unittest.TestCase):
    def test_all_success_dynamic_split_carries_unused(self):
        # 预算 100，三段等权；每段实际只用 10，剩余全部向后传递
        result, _ = chain(100, [make_stage("A", [10]), make_stage("B", [10]),
                                make_stage("C", [10])])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.values, ["A:ok#1", "B:ok#1", "C:ok#1"])
        self.assertEqual(result.elapsed_ms, 30)
        self.assertEqual(result.remaining_ms, 70)

    def test_weights_are_proportional(self):
        result, _ = chain(100, [make_stage("heavy", [1000], weight=3),
                                make_stage("light", [1], weight=1)])
        # 首段挂死，其授权 = 100 * 3/4 = 75；段内超时，剩余 25 预算显式返回
        self.assertEqual(result.status, "stage_timeout")
        self.assertEqual(result.stage_name, "heavy")
        self.assertAlmostEqual(result.remaining_ms, 25.0, places=6)
        grant = next(e["grant_ms"] for e in result.events if e["kind"] == "attempt_start")
        self.assertAlmostEqual(grant, 75.0, places=6)

    def test_first_stage_timeout_aborts_chain(self):
        # 首段挂死；授权被硬上限压到 20，超时后仍剩 80 预算 -> 显式 stage_timeout，
        # 后续段不被调度（不指望它们各自兜底）
        result, _ = chain(100, [
            make_stage("A", ["timeout"], max_attempt_ms=20),
            make_stage("B", [1]),
            make_stage("C", [1]),
        ])
        self.assertEqual(result.status, "stage_timeout")
        self.assertEqual(result.stage_name, "A")
        self.assertEqual(result.values, [])
        self.assertEqual(result.attempts_by_stage, [1])
        self.assertGreater(result.remaining_ms, 0)
        enters = [e["stage"] for e in result.events if e["kind"] == "stage_enter"]
        self.assertEqual(enters, ["A"])

    def test_last_stage_timeout_with_remaining_budget(self):
        result, _ = chain(100, [
            make_stage("A", [10]),
            make_stage("B", [10]),
            make_stage("C", ["timeout"], max_attempt_ms=30),
        ])
        self.assertEqual(result.status, "stage_timeout")
        self.assertEqual(result.stage_index, 2)
        self.assertEqual(result.remaining_ms, 50)

    def test_last_stage_hits_global_deadline(self):
        # 前两段各用 30，末段挂死其切片 40，预算正好耗尽 -> exhausted
        result, _ = chain(100, [
            make_stage("A", [30]),
            make_stage("B", [30]),
            make_stage("C", ["timeout"]),
        ])
        self.assertEqual(result.status, "exhausted")
        self.assertEqual(result.stage_name, "C")
        self.assertAlmostEqual(result.remaining_ms, 0.0, places=6)
        self.assertEqual(result.reason, "global_deadline_after_attempt_timeout")

    def test_retry_consumes_one_budget_and_cannot_double(self):
        # 单段 3 次尝试，退避 5,10；朴素重试会用掉 ~3*独立超时，这里总耗时锁死在 100
        result, _ = chain(100, [
            make_stage("S", ["timeout", "timeout", "timeout"],
                       max_attempts=3, backoff_base_ms=5, backoff_factor=2),
        ])
        self.assertEqual(result.status, "exhausted")
        self.assertEqual(result.attempts_by_stage, [3])
        # 预算消耗序列：33.33 -> +5 退避 -> 30.83 -> +10 退避 -> 20.83 = 100
        starts = [e["at_ms"] for e in result.events if e["kind"] == "attempt_start"]
        self.assertEqual([round(x, 2) for x in starts], [0.0, 38.33, 79.17])
        grants = [e["grant_ms"] for e in result.events if e["kind"] == "attempt_start"]
        self.assertEqual([round(x, 2) for x in grants], [33.33, 30.83, 20.83])
        self.assertAlmostEqual(result.elapsed_ms, 100.0, places=6)
        for event in result.events:
            self.assertLessEqual(event["at_ms"], 100.0 + 1e-9)

    def test_retry_then_success_within_budget(self):
        result, _ = chain(100, [
            make_stage("S", ["timeout", 10], max_attempts=2, backoff_base_ms=5),
        ])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.attempts_by_stage, [2])
        self.assertAlmostEqual(result.elapsed_ms, 100 / 2 + 5 + 10, places=6)

    def test_queue_wait_counts_against_budget(self):
        result, _ = chain(100, [make_stage("A", [70])], queue=30)
        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.remaining_ms, 0.0, places=6)

        result2, _ = chain(100, [make_stage("A", ["timeout"])], queue=100)
        self.assertEqual(result2.status, "exhausted")
        self.assertEqual(result2.reason, "budget_exhausted_before_stage")
        self.assertEqual(result2.values, [])

    def test_exhausted_is_never_silent(self):
        # A 用掉 99，B 只剩 1ms 可用但 min_attempt_ms=2：不发起调用，显式 exhausted
        result, _ = chain(100, [
            make_stage("A", [99], weight=99),
            make_stage("B", [1], weight=1, min_attempt_ms=2.0),
        ])
        self.assertEqual(result.status, "exhausted")
        self.assertEqual(result.stage_name, "B")
        self.assertEqual(result.reason, "below_min_attempt_ms")
        self.assertAlmostEqual(result.remaining_ms, 1.0, places=6)

    def test_non_timeout_error_is_explicit(self):
        def boom(ctx, grant):
            raise ValueError("bad payload")

        result, _ = chain(100, [
            Stage("A", boom),
            make_stage("B", [1]),
        ])
        self.assertEqual(result.status, "stage_error")
        self.assertEqual(result.error, "ValueError: bad payload")
        enters = [e["stage"] for e in result.events if e["kind"] == "stage_enter"]
        self.assertEqual(enters, ["A"])

    def test_clock_rollback_does_not_inflate_budget(self):
        clock = FakeClock(0)
        handler_clock[0] = clock
        budget = TimeBudget.start(100, clock=clock, sleep=clock.sleep)

        clock.advance(40)
        self.assertEqual(budget.remaining_ms(), 60)  # 预算观察到高水位 40
        clock.set(10)  # 回拨 30ms
        # 包络口径：剩余仍按高水位 40 算，是 60 而不是 90，预算不会被回拨放大
        self.assertEqual(budget.remaining_ms(), 60)
        self.assertEqual(budget.elapsed_ms(), 40)

    def test_clock_rollback_after_exhaustion_stays_exhausted(self):
        clock = FakeClock(0)
        handler_clock[0] = clock
        budget = TimeBudget.start(100, clock=clock, sleep=clock.sleep)
        clock.advance(100)
        self.assertTrue(budget.is_exhausted())
        clock.set(0)  # 恶意/异常回拨
        self.assertTrue(budget.is_exhausted())

    def test_clock_jump_forward_exhausts(self):
        clock = FakeClock(0)
        handler_clock[0] = clock
        budget = TimeBudget.start(100, clock=clock, sleep=clock.sleep)

        def slow_machine(ctx, grant):
            clock.advance(120)  # 调度停顿/STW 式前跳
            return "late"

        result = run_chain(budget, [Stage("A", slow_machine), make_stage("B", [1])])
        self.assertEqual(result.status, "exhausted")
        self.assertEqual(result.reason, "handler_overran_global_deadline")
        enters = [e["stage"] for e in result.events if e["kind"] == "stage_enter"]
        self.assertEqual(enters, ["A"])

    def test_misbehaving_handler_overruns_deadline_is_explicit(self):
        # 不合作的段函数忽略授权时间且越过全局 deadline：显式 exhausted，后续不调度
        def rogue(ctx, grant):
            handler_clock[0].sleep(grant + 60)
            return "too late"

        result, _ = chain(100, [
            Stage("A", rogue),
            make_stage("B", [1]),
        ])
        self.assertEqual(result.status, "exhausted")
        self.assertEqual(result.reason, "handler_overran_global_deadline")
        self.assertEqual(result.values, ["too late"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Edge-case tests for the timeout-budget chain.

All time is virtual and injected; no test sleeps in real time.
"""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeout_budget import (
    AbortError,
    Budget,
    BudgetExhausted,
    ChainResult,
    EventType,
    MonotonicClock,
    Outcome,
    Stage,
    StageError,
    SliceTimeout,
    VirtualClock,
    as_monotonic,
    run,
)


def scripted_handler(costs, clock, result=None):
    """Handler that advances the virtual clock by a scripted cost per call.

    ``costs`` entries:
      * int n          -> spend n ticks, return ``result``
      * "timeout"      -> spend the whole slice, raise SliceTimeout
      * ("timeout", n) -> spend n ticks, raise SliceTimeout
      * ("abort", msg) -> raise AbortError (permanent)
      * ("err", msg)   -> raise retryable StageError
      * ("overrun", n) -> spend n ticks (more than the slice), return
    """
    calls = {"n": 0}

    def handler(ctx):
        i = calls["n"]
        calls["n"] += 1
        spec = costs[i] if i < len(costs) else costs[-1]
        if spec == "timeout":
            clock.advance(ctx.slice_ticks)
            raise SliceTimeout("scripted timeout")
        if isinstance(spec, tuple):
            kind = spec[0]
            if kind == "timeout":
                clock.advance(spec[1])
                raise SliceTimeout("scripted timeout")
            if kind == "abort":
                raise AbortError(spec[1])
            if kind == "err":
                raise StageError(spec[1], retryable=True)
            if kind == "overrun":
                clock.advance(spec[1])
                return result
            raise AssertionError("unknown spec {}".format(spec))
        clock.advance(spec)
        return result

    handler.calls = calls
    return handler


def make_chain(specs, budget, **run_kwargs):
    """specs: list of (name, weight, max_attempts, backoff, costs)."""
    clock = budget.clock
    stages = [
        Stage(
            name=name,
            weight=weight,
            max_attempts=max_attempts,
            backoff_ticks=backoff,
            handler=scripted_handler(costs, clock, result=name),
        )
        for name, weight, max_attempts, backoff, costs in specs
    ]
    return run(stages, budget, **run_kwargs)


class TestSplitRule(unittest.TestCase):
    def test_equal_weights_floor_rounding(self):
        budget = Budget(100, VirtualClock(0))
        self.assertEqual(budget.slice_for(1, 3), 33)
        self.assertEqual(budget.slice_for(1, 2), 50)
        self.assertEqual(budget.slice_for(1, 1), 100)

    def test_weighted_split_uses_remaining(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)
        self.assertEqual(budget.slice_for(2, 5), 40)  # floor(100*2/5)
        clock.advance(40)
        self.assertEqual(budget.slice_for(2, 3), 40)  # floor(60*2/3)
        clock.advance(40)
        self.assertEqual(budget.slice_for(1, 1), 20)  # everything left

    def test_invalid_weights_rejected(self):
        budget = Budget(100, VirtualClock(0))
        for args in [(0, 3), (1, 0), (4, 3), (-1, 3)]:
            with self.assertRaises(ValueError):
                budget.slice_for(*args)

    def test_sub_floor_slice_is_exhaustion(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)
        clock.advance(99)
        with self.assertRaises(BudgetExhausted):
            budget.slice_for(1, 3)  # floor(1/3) == 0 < min_slice

    def test_budget_must_be_positive(self):
        with self.assertRaises(ValueError):
            Budget(0, VirtualClock(0))


class TestAllSuccess(unittest.TestCase):
    def test_all_success_returns_leftover(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)
        result = make_chain(
            [
                ("A", 1, 1, 0, [20]),
                ("B", 1, 1, 0, [10]),
                ("C", 1, 1, 0, [15]),
            ],
            budget,
        )
        self.assertEqual(result.outcome, Outcome.SUCCESS)
        self.assertEqual(result.used, 45)
        self.assertEqual(result.remaining, 55)
        self.assertEqual(result.attempts_total, 3)
        self.assertEqual(result.value, "C")
        # slices actually handed out: 33, 33, 34 (sum == 100, never more)
        slices = [e.duration for e in result.events]  # noqa: F841
        stage_events = [e for e in result.events if e.type is EventType.STAGE]
        self.assertEqual(len(stage_events), 3)
        self.assertEqual([e.duration for e in stage_events], [20, 10, 15])

    def test_weighted_success(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)
        result = make_chain(
            [
                ("A", 2, 1, 0, [30]),
                ("B", 1, 1, 0, [10]),
            ],
            budget,
        )
        self.assertEqual(result.outcome, Outcome.SUCCESS)
        self.assertEqual(result.used, 40)


class TestFirstStageTimeout(unittest.TestCase):
    def test_first_stage_timeout_retries_share_one_budget(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)
        result = make_chain(
            [
                ("A", 1, 12, 0, ["timeout"]),
                ("B", 1, 1, 0, [1]),
                ("C", 1, 1, 0, [1]),
            ],
            budget,
        )
        self.assertEqual(result.outcome, Outcome.BUDGET_EXHAUSTED)
        # Later stages must never be dispatched.
        stage_events = [e for e in result.events if e.type is EventType.STAGE]
        self.assertTrue(all(e.stage_index == 0 for e in stage_events))
        # The budget is the ceiling: 11 attempts * slices sum to 98 <= 100.
        self.assertEqual(result.attempts_total, 11)
        self.assertEqual(result.used, 98)
        self.assertLessEqual(result.used, result.total)
        self.assertLess(result.used, 2 * result.total)

    def test_first_stage_timeout_with_backoff(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)
        result = make_chain(
            [
                ("A", 1, 12, 2, ["timeout"]),
                ("B", 1, 1, 0, [1]),
                ("C", 1, 1, 0, [1]),
            ],
            budget,
        )
        self.assertEqual(result.outcome, Outcome.BUDGET_EXHAUSTED)
        self.assertEqual(result.attempts_total, 7)
        self.assertEqual(result.used, 99)
        backoffs = [e for e in result.events if e.type is EventType.BACKOFF]
        self.assertEqual(len(backoffs), 7)
        self.assertTrue(all(e.duration == 2 for e in backoffs))

    def test_attempt_cap_can_end_before_budget(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)
        result = make_chain(
            [
                ("A", 1, 3, 0, ["timeout"]),
                ("B", 1, 1, 0, [1]),
                ("C", 1, 1, 0, [1]),
            ],
            budget,
        )
        self.assertEqual(result.outcome, Outcome.ATTEMPTS_EXHAUSTED)
        self.assertEqual(result.attempts_total, 3)
        self.assertEqual(result.used, 70)  # 33 + 22 + 15


class TestLastStageTimeout(unittest.TestCase):
    def test_last_stage_gets_all_remaining_and_stops(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)
        result = make_chain(
            [
                ("A", 1, 1, 0, [10]),
                ("B", 1, 1, 0, [10]),
                ("C", 1, 3, 0, ["timeout"]),
            ],
            budget,
        )
        self.assertEqual(result.outcome, Outcome.BUDGET_EXHAUSTED)
        # The last stage owns 100% of remaining weight: one 80-tick slice,
        # no budget left for a second attempt.
        self.assertEqual(result.attempts_total, 3)
        self.assertEqual(result.used, 100)
        self.assertEqual(result.remaining, 0)

    def test_last_stage_timeout_then_success(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)
        result = make_chain(
            [
                ("A", 1, 1, 0, [10]),
                ("B", 1, 1, 0, [10]),
                ("C", 1, 2, 0, [("timeout", 50), 20]),
            ],
            budget,
        )
        self.assertEqual(result.outcome, Outcome.SUCCESS)
        self.assertEqual(result.used, 90)


class TestQueueingCounts(unittest.TestCase):
    def test_queue_before_dispatch_is_charged(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)  # created at enqueue time
        clock.advance(80)  # request sat in a queue
        result = make_chain(
            [
                ("A", 1, 1, 0, [10]),
                ("B", 1, 1, 0, [5]),
            ],
            budget,
        )
        self.assertEqual(result.outcome, Outcome.SUCCESS)
        self.assertEqual(result.used, 95)
        queue_events = [e for e in result.events if e.type is EventType.QUEUE]
        self.assertEqual(len(queue_events), 1)
        self.assertEqual(queue_events[0].duration, 80)

    def test_queue_can_eat_the_whole_budget(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)
        clock.advance(100)
        called = []

        def handler(ctx):
            called.append(ctx.stage_index)
            return None

        result = run(
            [Stage("A", handler=handler), Stage("B", handler=handler)],
            budget,
        )
        self.assertEqual(result.outcome, Outcome.BUDGET_EXHAUSTED)
        self.assertEqual(called, [])  # nothing was dispatched


class TestEarlyAbort(unittest.TestCase):
    def test_later_stages_never_dispatched_after_exhaustion(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)
        clock.advance(60)
        called = []

        def slow(ctx):
            called.append(ctx.stage_index)
            clock.advance(ctx.slice_ticks)
            raise SliceTimeout()

        def spy(ctx):
            called.append(ctx.stage_index)
            return None

        result = run(
            [
                Stage("A", weight=1, max_attempts=10, handler=slow),
                Stage("B", weight=1, handler=spy),
                Stage("C", weight=1, handler=spy),
            ],
            budget,
        )
        self.assertEqual(result.outcome, Outcome.BUDGET_EXHAUSTED)
        self.assertTrue(all(i == 0 for i in called))


class TestClockRollback(unittest.TestCase):
    def test_rollback_grants_no_extra_budget(self):
        raw = VirtualClock(0)
        clock = MonotonicClock(raw)
        budget = Budget(100, clock)

        def handler(ctx):
            raw.advance(20)
            ctx.now()  # cooperative sampling: monotonic view observes t=20
            raw.rollback(15)  # wall clock jumps backwards mid-flight
            return "done"

        result = run([Stage("A", handler=handler)], budget)
        self.assertEqual(result.outcome, Outcome.SUCCESS)
        # Elapsed is measured on the monotonic view: rollback counts as 0.
        self.assertEqual(result.used, 20)
        self.assertEqual(budget.remaining(), 80)

    def test_rollback_cannot_unspend_budget(self):
        raw = VirtualClock(0)
        clock = MonotonicClock(raw)
        budget = Budget(30, clock)

        costs = {}

        def handler(ctx):
            if ctx.attempt == 1:
                # Burn the 15-tick slice, then rewind the raw clock fully.
                raw.advance(15)
                ctx.budget.now()  # spend is sampled before the rewind
                raw.rollback(15)
                costs.setdefault(ctx.stage_index, 1)
                raise SliceTimeout("still timed out")
            # Second attempt only costs 1 tick on the raw clock.
            raw.advance(1)
            costs[ctx.stage_index] = costs.get(ctx.stage_index, 0) + 1
            return "ok"

        result = run(
            [
                Stage("A", max_attempts=3, handler=handler),
                Stage("B", handler=lambda c: None),
            ],
            budget,
        )
        # Chain succeeds, but the 15 spent ticks were NOT given back:
        # accounting stays at the frozen high-water mark 15 (remaining 15),
        # instead of resetting to raw time 1 (which would give remaining 29).
        self.assertEqual(result.outcome, Outcome.SUCCESS)
        self.assertEqual(result.used, 15)
        self.assertEqual(result.remaining, 15)

    def test_rollback_loop_never_inflates_budget(self):
        raw = VirtualClock(0)
        clock = MonotonicClock(raw)
        budget = Budget(30, clock)

        def sneaky(ctx):
            raw.advance(ctx.slice_ticks)
            ctx.budget.now()
            raw.rollback(ctx.slice_ticks)
            raise SliceTimeout("still timed out")

        result = run(
            [
                Stage("A", max_attempts=50, handler=sneaky),
                Stage("B", handler=lambda c: None),
            ],
            budget,
        )
        # Attempts are still capped; elapsed is a frozen high-water mark and
        # can never exceed the total budget.
        self.assertEqual(result.outcome, Outcome.ATTEMPTS_EXHAUSTED)
        self.assertLessEqual(result.used, 30)
        self.assertTrue(all(e.stage_index == 0 for e in result.events
                            if e.type is EventType.STAGE))

    def test_pure_rollback_freezes_monotonic_view(self):
        raw = VirtualClock(0)
        clock = MonotonicClock(raw)
        budget = Budget(100, clock)
        raw.rollback(50)
        self.assertEqual(budget.elapsed(), 0)
        self.assertEqual(budget.remaining(), 100)

    def test_forward_jump_burns_budget(self):
        raw = VirtualClock(0)
        clock = MonotonicClock(raw)
        budget = Budget(100, clock)
        raw.advance(500)  # NTP step / suspend-resume
        result = run([Stage("A", handler=lambda c: None)], budget)
        self.assertEqual(result.outcome, Outcome.BUDGET_EXHAUSTED)
        self.assertEqual(result.remaining, 0)

    def test_as_monotonic_is_idempotent(self):
        clock = MonotonicClock(VirtualClock(0))
        self.assertIs(as_monotonic(clock), clock)


class TestOverrunEnforcement(unittest.TestCase):
    def test_handler_ignoring_slice_is_reported_as_exhaustion(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)
        result = make_chain(
            [
                ("A", 1, 1, 0, [("overrun", 150)]),  # slice was 100
                ("B", 1, 1, 0, [1]),
            ],
            budget,
        )
        self.assertEqual(result.outcome, Outcome.BUDGET_EXHAUSTED)
        self.assertIn("ignored its 50-tick slice", result.reason)

    def test_permanent_error_is_not_retried(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)
        result = make_chain(
            [
                ("A", 1, 5, 0, [("abort", "bad request")]),
                ("B", 1, 1, 0, [1]),
            ],
            budget,
        )
        self.assertEqual(result.outcome, Outcome.STAGE_ERROR)
        self.assertEqual(result.attempts_total, 1)
        self.assertEqual(result.error, "bad request")

    def test_handler_can_signal_exhaustion_explicitly(self):
        clock = VirtualClock(0)
        budget = Budget(100, clock)

        def handler(ctx):
            clock.advance(10)
            raise BudgetExhausted("downstream said deadline exceeded")

        result = run([Stage("A", handler=handler)], budget)
        self.assertEqual(result.outcome, Outcome.BUDGET_EXHAUSTED)
        self.assertIn("downstream", result.reason)


class TestNeverSilent(unittest.TestCase):
    def test_every_terminal_state_is_explicit(self):
        for outcome in Outcome:
            self.assertIsInstance(outcome.value, str)
        clock = VirtualClock(0)
        budget = Budget(10, clock)
        result = make_chain([("A", 1, 1, 0, [1])], budget)
        self.assertIsInstance(result, ChainResult)
        self.assertTrue(result.reason)


class TestInvariantFuzz(unittest.TestCase):
    def test_used_never_exceeds_total(self):
        rng = random.Random(20261005)
        for trial in range(300):
            total = rng.randint(1, 200)
            n_stages = rng.randint(1, 5)
            clock = VirtualClock(0)
            budget = Budget(total, clock)
            specs = []
            for i in range(n_stages):
                costs = []
                for _ in range(rng.randint(1, 4)):
                    r = rng.random()
                    if r < 0.3:
                        costs.append("timeout")
                    elif r < 0.4:
                        costs.append(("timeout", rng.randint(0, 50)))
                    else:
                        costs.append(rng.randint(0, 40))
                specs.append(
                    (
                        "s{}".format(i),
                        rng.randint(1, 4),
                        rng.randint(1, 5),
                        rng.choice([0, 1, 3]),
                        costs,
                    )
                )
            result = make_chain(specs, budget)
            self.assertLessEqual(
                result.used,
                result.total,
                "trial {}: used {} > total {}".format(
                    trial, result.used, result.total
                ),
            )
            self.assertGreaterEqual(result.remaining, 0)
            self.assertEqual(result.used + result.remaining, result.total)


if __name__ == "__main__":
    unittest.main()

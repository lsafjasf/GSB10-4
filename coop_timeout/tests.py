"""Self-tests for cooperative interruption (stdlib unittest only).

Coverage:
  * not-timed-out / exactly-at-deadline / just-under-deadline boundaries
  * outcome distinguishability: ok vs timeout vs cancelled vs failed
  * cleanup result for interrupt points in every phase
  * re-interrupt during cleanup (new signal + repeated signal)
  * completed/failed task interrupted afterwards (A/B paired data)
  * atomicity of steps/acquires/releases (exactly-once, LIFO)
  * release failure containment
  * full demo scenario matrix
"""

from __future__ import annotations

import unittest
from typing import List

from . import demo
from .coop import (
    CANCELLED,
    FAILED,
    OK,
    SIG_CANCEL,
    SIG_TIMEOUT,
    TIMEOUT,
    CancellationToken,
    SystemClock,
    TaskContext,
    VirtualClock,
    run,
)


class BoundaryTests(unittest.TestCase):
    def _mini_task(self, events):
        def task(ctx: TaskContext) -> str:
            ctx.step("w", 10, lambda: events.append("w"))
            return "done"
        return task

    def test_not_timed_out(self):
        for deadline in (11, 1000):
            clock, token, events = VirtualClock(), CancellationToken(), []
            r = run(self._mini_task(events), clock=clock, token=token, deadline=deadline)
            self.assertEqual(r.outcome, OK)
            self.assertEqual(r.value, "done")
            self.assertIsNone(r.overshoot)

    def test_exactly_at_deadline_is_timeout(self):
        clock, events = VirtualClock(), []
        r = run(self._mini_task(events), clock=clock, deadline=10)
        self.assertEqual(r.outcome, TIMEOUT)  # post-w checkpoint: now == deadline
        self.assertEqual(r.stage, "post-w")
        self.assertEqual(r.overshoot, 0)

    def test_just_under_deadline_is_ok(self):
        clock, events = VirtualClock(), []
        r = run(self._mini_task(events), clock=clock, deadline=11)
        self.assertEqual(r.outcome, OK)

    def test_cancel_before_start(self):
        clock, token = VirtualClock(), CancellationToken()
        token.cancel()
        called = []
        def task(ctx):
            ctx.checkpoint("start")  # first safe checkpoint honors the latch
            called.append(1)
        r = run(task, clock=clock, token=token)
        self.assertEqual(r.outcome, CANCELLED)
        self.assertEqual(r.stage, "start")
        self.assertEqual(called, [])


class DistinguishabilityTests(unittest.TestCase):
    def test_four_outcomes_are_distinguishable(self):
        def ok_task(ctx):
            ctx.step("w", 1)
            return 42

        def fail_task(ctx):
            ctx.checkpoint("pre-w")
            raise KeyError("k")

        clock = VirtualClock()
        self.assertEqual(run(ok_task, clock=VirtualClock(), deadline=100).outcome, OK)
        r_to = run(ok_task, clock=VirtualClock(), deadline=1)
        self.assertEqual(r_to.outcome, TIMEOUT)
        self.assertEqual(r_to.signals, (SIG_TIMEOUT,))

        token = CancellationToken()
        def cancel_at(ctx, where):
            if where == "pre-w":
                token.cancel()
        r_ca = run(ok_task, clock=VirtualClock(), token=token, probe=cancel_at)
        self.assertEqual(r_ca.outcome, CANCELLED)
        self.assertEqual(r_ca.signals, (SIG_CANCEL,))

        r_fa = run(fail_task, clock=VirtualClock())
        self.assertEqual(r_fa.outcome, FAILED)
        self.assertIn("KeyError", r_fa.error)
        self.assertEqual(r_fa.stage, "pre-w")

    def test_cancel_and_timeout_at_same_checkpoint(self):
        token = CancellationToken()
        def probe(ctx, where):
            if where == "pre-w":
                token.cancel()
        def task(ctx):
            ctx.step("w", 10)
        r = run(task, clock=VirtualClock(), token=token, deadline=0, probe=probe)
        self.assertEqual(r.outcome, CANCELLED)  # cancel precedence
        self.assertEqual(r.signals, (SIG_CANCEL, SIG_TIMEOUT))


class AtomicityTests(unittest.TestCase):
    def test_signal_during_step_is_latched_step_completes(self):
        token = CancellationToken()
        events = []

        def body():
            events.append("step-start")
            token.cancel()          # signal in the MIDDLE of an atomic step
            events.append("step-end")  # must still run

        def task(ctx):
            ctx.step("w", 10, body)

        r = run(task, clock=VirtualClock(), token=token)
        self.assertEqual(r.outcome, CANCELLED)
        self.assertEqual(r.stage, "post-w")
        self.assertEqual(events, ["step-start", "step-end"])

    def test_signal_during_acquire_latched_resource_registered_and_cleaned(self):
        events = []

        token = CancellationToken()
        class _Clock(VirtualClock):
            def advance(self, delta):
                super().advance(delta)
                token.cancel()  # signal fires in virtual time DURING acquire

        def task(ctx):
            def rel():
                events.append("released")
            ctx.acquire("R", rel, duration=5)

        r = run(task, clock=_Clock(), token=token)
        self.assertEqual(r.outcome, CANCELLED)
        self.assertEqual(r.stage, "post-acquire-R")
        self.assertEqual(events, ["released"])
        self.assertTrue(r.cleanup.all_released)

    def test_releases_are_lifo_exactly_once(self):
        order = []
        def make_rel(name):
            return lambda: order.append(name)

        def task(ctx):
            ctx.acquire("R1", make_rel("R1"))
            ctx.acquire("R2", make_rel("R2"))
            ctx.acquire("R3", make_rel("R3"))
            ctx.checkpoint("pre-fail")
            raise RuntimeError("x")

        r = run(task, clock=VirtualClock())
        self.assertEqual(r.outcome, FAILED)
        self.assertEqual(order, ["R3", "R2", "R1"])
        self.assertTrue(r.cleanup.all_released)
        self.assertEqual([x.name for x in r.cleanup.records], ["R1", "R2", "R3"])

    def test_release_failure_does_not_block_remaining_releases(self):
        order = []

        def bad():
            order.append("R2-tried")
            raise OSError("boom")

        def good_r1():
            order.append("R1")

        def good_r3():
            order.append("R3")

        def task(ctx):
            ctx.acquire("R1", good_r1)
            ctx.acquire("R2", bad)
            ctx.acquire("R3", good_r3)
            ctx.checkpoint("pre-fail")
            raise RuntimeError("x")

        r = run(task, clock=VirtualClock())
        self.assertEqual(r.outcome, FAILED)
        self.assertEqual(order, ["R3", "R2-tried", "R1"])  # LIFO continues
        self.assertTrue(r.cleanup.all_released)  # "released" means attempted/recorded
        self.assertEqual(r.cleanup.release_errors[0][0], "R2")
        self.assertIn("OSError", r.cleanup.release_errors[0][1])

    def test_signal_during_release_cannot_split_it(self):
        token = CancellationToken()
        events = []

        def rel():
            events.append("rel-start")
            token.cancel()          # re-interrupt in the middle of a release
            events.append("rel-end")  # release still runs to completion

        def task(ctx):
            ctx.acquire("R", rel)
            token.cancel()  # already cancelled before? no: set at first checkpoint?
            ctx.checkpoint("pre-fail")

        # cancel is requested before pre-fail checkpoint -> interrupted there,
        # and release also re-cancels (idempotent); release must be complete
        r = run(task, clock=VirtualClock(), token=token)
        self.assertEqual(r.outcome, CANCELLED)
        self.assertEqual(events, ["rel-start", "rel-end"])


class ReInterruptDuringCleanupTests(unittest.TestCase):
    def _cleanup_task(self, events, clock, token, *, deadline, second_signal):
        def task(ctx):
            def rel(name):
                def _r():
                    events.append("rel-" + name)
                    clock.advance(10)
                    if second_signal == "cancel":
                        token.cancel()
                return _r

            ctx.acquire("A", rel("A"))
            ctx.acquire("B", rel("B"))
            ctx.checkpoint("pre-w")

        def probe(ctx, where):
            if where == "pre-w":
                token.cancel()  # first interrupt: cancel

        return run(task, clock=clock, token=token, deadline=deadline, probe=probe)

    def test_new_signal_during_cleanup_observed_cleanup_completes(self):
        clock, token, events = VirtualClock(), CancellationToken(), []
        r = self._cleanup_task(events, clock, token, deadline=10, second_signal=None)
        self.assertEqual(r.outcome, CANCELLED)
        self.assertEqual(events, ["rel-B", "rel-A"])  # cleanup not aborted
        obs = [(o.signal, o.stage) for o in r.cleanup.observations]
        self.assertEqual(obs, [(SIG_TIMEOUT, "post-release-B")])
        self.assertTrue(r.cleanup.all_released)
        self.assertEqual(r.signals, (SIG_CANCEL, SIG_TIMEOUT))

    def test_repeated_same_signal_during_cleanup_is_idempotent_no_observation(self):
        clock, token, events = VirtualClock(), CancellationToken(), []
        r = self._cleanup_task(events, clock, token, deadline=None,
                               second_signal="cancel")
        self.assertEqual(r.outcome, CANCELLED)
        self.assertEqual(r.cleanup.observations, [])  # already known signal
        self.assertEqual(events, ["rel-B", "rel-A"])


class CompletedThenInterruptedTests(unittest.TestCase):
    def setUp(self):
        self.success = next(s for s in demo.scenarios() if s.name == "K-success")
        self.failure = next(s for s in demo.scenarios() if s.name == "I-fail-mid-w2")

    def _late_variant(self, base):
        sc = demo.Scenario(base.name + "+late-cancel", late_cancel=True,
                           deadline=base.deadline, probe_cancel_at=base.probe_cancel_at,
                           pre_cancel=base.pre_cancel, in_step_cancel_at=base.in_step_cancel_at,
                           cancel_mid_release=base.cancel_mid_release, fail_at=base.fail_at,
                           release_fail=base.release_fail, release_duration=base.release_duration,
                           expect_outcome=base.expect_outcome, expect_stage=base.expect_stage,
                           expect_released=base.expect_released,
                           expect_signals=base.expect_signals,
                           expect_overshoot=base.expect_overshoot,
                           expect_observations=base.expect_observations,
                           expect_release_error=base.expect_release_error)
        return sc

    def test_cancel_after_completion_does_not_change_result(self):
        r0, ev0, _, _ = demo.run_scenario(self.success)
        r1, ev1, tok, _ = demo.run_scenario(self._late_variant(self.success))
        self.assertTrue(tok.cancelled)
        self.assertEqual(r0.key(), r1.key())
        self.assertEqual(r0.cleanup, r1.cleanup)
        self.assertEqual(ev0, ev1)

    def test_cancel_after_failure_does_not_change_result(self):
        r0, ev0, _, _ = demo.run_scenario(self.failure)
        r1, ev1, tok, _ = demo.run_scenario(self._late_variant(self.failure))
        self.assertTrue(tok.cancelled)
        self.assertEqual(r0.key(), r1.key())
        self.assertEqual(r0.cleanup, r1.cleanup)
        self.assertEqual(ev0, ev1)

    def test_late_cancel_returns_same_object_data_paired(self):
        # explicit A/B paired data used in the report
        r0, _, _, _ = demo.run_scenario(self.success)
        r1, _, _, _ = demo.run_scenario(self._late_variant(self.success))
        a = (r0.outcome, r0.value, r0.stage, r0.signals,
             [(x.name, x.released_at, x.release_error) for x in r0.cleanup.records])
        b = (r1.outcome, r1.value, r1.stage, r1.signals,
             [(x.name, x.released_at, x.release_error) for x in r1.cleanup.records])
        self.assertEqual(a, b)
        self.assertEqual(a, ("ok", "RESULT", "commit", (),
                             [("R1", 30, None), ("R2", 30, None)]))


class ScenarioMatrixTests(unittest.TestCase):
    def test_all_demo_scenarios(self):
        for sc in demo.scenarios():
            with self.subTest(scenario=sc.name):
                r = demo.check_scenario(sc)
                self.assertTrue(r.cleanup.all_released)

    def test_success_value_and_commit_event(self):
        r, events, _, _ = demo.run_scenario(
            next(s for s in demo.scenarios() if s.name == "K-success"))
        self.assertEqual(r.value, "RESULT")
        self.assertIn("commit", events)

    def test_timeout_scenario_commit_never_runs(self):
        for name in ("F-timeout-eq-post-w3", "G0-cancel-at-pre-commit"):
            _, events, _, _ = demo.run_scenario(
                next(s for s in demo.scenarios() if s.name == name))
            self.assertNotIn("commit", events, name)


class ClockTests(unittest.TestCase):
    def test_virtual_clock(self):
        c = VirtualClock(5)
        self.assertEqual(c.now(), 5)
        c.advance(3)
        self.assertEqual(c.now(), 8)

    def test_system_clock_monotonic(self):
        c = SystemClock()
        a, b = c.now(), c.now()
        self.assertLessEqual(a, b)
        with self.assertRaises(TypeError):
            c.advance(1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

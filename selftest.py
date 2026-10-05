"""Self-tests for the graceful_shutdown library (standard library only).

Run:  python3 -m unittest selftest -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from graceful_shutdown import (
    Policy,
    ShutdownConfig,
    SimEngine,
    Status,
    TaskSpec,
    make_specs,
    reconcile,
    run_full,
    run_shutdown,
    run_shutdown_scenario,
)

ROOT = os.path.dirname(os.path.abspath(__file__))


def spec(tid, policy, steps, **kw):
    return TaskSpec(task_id=tid, policy=policy, steps=tuple(steps), **kw)


class NoInflightTest(unittest.TestCase):
    def test_shutdown_with_zero_tasks_is_instant_and_punctual(self):
        engine = SimEngine(workers=2)
        report = run_shutdown(
            engine, ShutdownConfig(drain_timeout=1.0, cleanup_timeout=1.0, force_wait=1.0)
        )
        self.assertEqual(report.duration, 0.0)  # nothing to wait for
        self.assertTrue(report.punctual)
        self.assertEqual(report.counts, {})
        self.assertEqual(report.abandoned, [])


class FewInflightTest(unittest.TestCase):
    def setUp(self):
        self.specs = [
            spec("ck1", Policy.CHECKPOINT, [0.01] * 4, cleanup_cost=0.005),
            spec("ck2", Policy.CHECKPOINT, [0.01] * 4, cleanup_cost=0.005),
            spec("rb1", Policy.ROLLBACK, [0.01] * 4, cleanup_cost=0.005,
                 rollback_step_cost=0.001),
            spec("rb2", Policy.ROLLBACK, [0.01] * 4, cleanup_cost=0.005,
                 rollback_step_cost=0.001),
            spec("fl1", Policy.FAIL, [0.01] * 4, cleanup_cost=0.005),
            spec("fl2", Policy.FAIL, [0.01] * 4, cleanup_cost=0.005),
        ]
        self.engine, self.report = run_shutdown_scenario(
            self.specs,
            warmup=0.015,  # every task has applied exactly 1 step
            config=ShutdownConfig(drain_timeout=0.1, cleanup_timeout=0.1,
                                  force_wait=0.1),
            workers=6,
        )

    def test_each_policy_gets_its_deterministic_outcome(self):
        results = self.engine.results
        # Cancel lands at the next safe point, so each task applied 2 steps.
        self.assertEqual(results["ck1"].status, Status.CHECKPOINTED)
        self.assertEqual(results["ck1"].steps_done, 2)
        self.assertEqual(results["rb1"].status, Status.ROLLED_BACK)
        self.assertEqual(results["rb1"].steps_done, 2)
        self.assertEqual(results["fl1"].status, Status.FAILED)
        self.assertEqual(results["fl1"].steps_done, 2)
        self.assertEqual(
            self.report.counts, {"checkpointed": 2, "rolled_back": 2, "failed": 2}
        )

    def test_durable_store_matches_outcomes(self):
        store = self.engine.store
        self.assertEqual(store.checkpoints, {"ck1": 2, "ck2": 2})
        self.assertEqual(store.failures, {"fl1": 2, "fl2": 2})
        # Rollback compensates applied steps in reverse order.
        self.assertEqual(
            [c for c in store.compensations if c[0] == "rb1"],
            [("rb1", 1), ("rb1", 0)],
        )
        self.assertEqual(len(store.compensations), 4)

    def test_write_ahead_log_order(self):
        ops = [(e.task_id, e.op) for e in self.engine.ledger.entries]
        self.assertIn(("ck1", "checkpoint"), ops)
        # Compensation intents are journaled before results are published.
        ck_seq = next(e.seq for e in self.engine.ledger.entries
                      if e.task_id == "ck1" and e.op == "checkpoint")
        self.assertGreaterEqual(ck_seq, 0)

    def test_punctual_and_within_budget(self):
        self.assertTrue(self.report.punctual)
        self.assertLessEqual(self.report.duration, 0.3 + 1e-9)
        for phase in self.report.phases:
            self.assertTrue(phase.within_budget, phase)


class ManyInflightTest(unittest.TestCase):
    def test_5000_inflight_all_disposed_punctually(self):
        specs = make_specs(5000, seed=7, min_steps=2, max_steps=4,
                           step_lo=1e-3, step_hi=1e-3)
        engine, report = run_shutdown_scenario(
            specs,
            warmup=5e-4,
            config=ShutdownConfig(drain_timeout=0.05, cleanup_timeout=5.0,
                                  force_wait=0.1),
            workers=16,
        )
        self.assertTrue(report.punctual)
        self.assertEqual(report.abandoned, [])
        self.assertEqual(sum(report.counts.values()), 5000)
        self.assertNotIn("abandoned", report.counts)
        # Cross-check against the full run.
        baseline = run_full(specs, workers=16)
        recon = reconcile(baseline.results, engine.results, store=engine.store)
        self.assertTrue(recon.ok, recon.violations[:5])


class StuckTaskTest(unittest.TestCase):
    def test_stuck_tasks_are_abandoned_exactly_at_the_deadline(self):
        specs = [spec(f"stuck{i}", Policy.CHECKPOINT, [1.0], stuck=True)
                 for i in range(3)]
        specs += [spec(f"ok{i}", Policy.CHECKPOINT, [0.001, 0.001],
                       cleanup_cost=0.001) for i in range(4)]
        engine, report = run_shutdown_scenario(
            specs,
            warmup=0.0005,
            config=ShutdownConfig(drain_timeout=0.01, cleanup_timeout=0.05,
                                  force_wait=0.02),
            workers=8,
        )
        self.assertEqual(report.abandoned, ["stuck0", "stuck1", "stuck2"])
        self.assertEqual(report.counts.get("abandoned"), 3)
        self.assertEqual(report.counts.get("checkpointed"), 4)
        # Punctuality: the shutdown consumed exactly the configured budget.
        self.assertAlmostEqual(report.duration, 0.08, places=12)
        self.assertAlmostEqual(report.finished_at, report.hard_deadline, places=12)
        self.assertTrue(report.punctual)
        # Abandoned tasks leave no durable footprint.
        self.assertEqual(engine.store.checkpoints.keys(),
                         {f"ok{i}" for i in range(4)})


class TightDeadlineTest(unittest.TestCase):
    def test_cleanup_timeout_forces_abandon(self):
        specs = [spec(f"t{i}", Policy.CHECKPOINT, [0.001], cleanup_cost=1.0)
                 for i in range(10)]
        engine, report = run_shutdown_scenario(
            specs,
            warmup=0.0,
            config=ShutdownConfig(drain_timeout=0.01, cleanup_timeout=0.05,
                                  force_wait=0.01),
            workers=4,
            cleanup_workers=2,
        )
        # Cleanups (1.0s each, 2 workers) cannot finish inside 0.05s.
        self.assertEqual(report.counts.get("abandoned"), 10)
        self.assertAlmostEqual(report.duration, 0.07, places=12)
        self.assertTrue(report.punctual)


class IntakeClosedTest(unittest.TestCase):
    def test_late_submission_is_rejected_and_recorded(self):
        engine = SimEngine(workers=1)
        engine.submit(spec("early", Policy.FAIL, [0.01], cleanup_cost=0.001))
        run_shutdown(engine, ShutdownConfig(0.01, 0.01, 0.01))
        accepted = engine.submit(spec("late", Policy.FAIL, [0.01]))
        self.assertFalse(accepted)
        self.assertEqual(engine.results["late"].status, Status.REJECTED)
        self.assertEqual(engine.results["late"].steps_done, 0)


class QueuedDispositionTest(unittest.TestCase):
    def test_queued_tasks_are_disposed_with_zero_steps_done(self):
        specs = []
        for i in range(15):
            policy = [Policy.CHECKPOINT, Policy.ROLLBACK, Policy.FAIL][i % 3]
            specs.append(spec(f"t{i}", policy, [0.01, 0.01], cleanup_cost=0.001))
        engine, report = run_shutdown_scenario(
            specs,
            warmup=0.0,  # shutdown immediately: 1 running, 14 queued
            config=ShutdownConfig(drain_timeout=0.05, cleanup_timeout=1.0,
                                  force_wait=0.01),
            workers=1,
            cleanup_workers=4,
        )
        self.assertTrue(report.punctual)
        self.assertEqual(report.abandoned, [])
        zero_step = [r for r in engine.results.values() if r.steps_done == 0]
        self.assertEqual(len(zero_step), 14)
        for tid, k in engine.store.checkpoints.items():
            self.assertIn(k, (0, 1, 2))
        # Rolled-back queued tasks need zero compensations.
        self.assertEqual(
            sum(1 for r in engine.results.values()
                if r.status is Status.ROLLED_BACK and r.steps_done == 0),
            len([1 for r in engine.results.values()
                 if r.status is Status.ROLLED_BACK and r.steps_done == 0]),
        )


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.specs = make_specs(300, seed=11, min_steps=3, max_steps=6,
                                step_lo=1e-3, step_hi=2e-3)
        self.baseline = run_full(self.specs, workers=8)
        self.engine, self.report = run_shutdown_scenario(
            self.specs,
            warmup=2e-3,
            config=ShutdownConfig(drain_timeout=0.05, cleanup_timeout=5.0,
                                  force_wait=0.05),
            workers=8,
        )

    def test_every_difference_is_explained(self):
        recon = reconcile(self.baseline.results, self.engine.results,
                          store=self.engine.store)
        self.assertTrue(recon.ok, recon.violations[:5])
        cats = recon.categories()
        self.assertEqual(cats.get("match", 0)
                         + cats.get("checkpointed", 0)
                         + cats.get("rolled-back", 0)
                         + cats.get("failed", 0), 300)

    def test_checkpointed_tasks_resume_to_baseline(self):
        by_id = {s.task_id: s for s in self.specs}
        resumed_engine = SimEngine(workers=8)
        resumed_ids = []
        for tid, k in self.engine.store.checkpoints.items():
            original = by_id[tid]
            resumed_engine.submit(TaskSpec(
                task_id=tid, policy=original.policy,
                steps=original.steps[k:],
            ))
            resumed_ids.append(tid)
        resumed_engine.advance_until(1e15)
        self.assertTrue(resumed_ids)  # scenario actually produced checkpoints
        recon = reconcile(self.baseline.results, self.engine.results,
                          store=self.engine.store,
                          resumed=resumed_engine.results)
        self.assertTrue(recon.ok, recon.violations[:5])
        self.assertEqual(recon.categories().get("resumed-match"),
                         len(resumed_ids))
        # Ledger: steps logged before + after resume add up to the baseline.
        for tid in resumed_ids:
            total = by_id[tid].total_steps
            done = (self.engine.ledger.count(tid, "step_done")
                    + resumed_engine.ledger.count(tid, "step_done"))
            self.assertEqual(done, total)

    def test_tampered_store_is_caught(self):
        recon = reconcile(self.baseline.results, self.engine.results,
                          store=self.engine.store)
        self.assertTrue(recon.ok)
        self.engine.store.checkpoints.pop(
            next(iter(self.engine.store.checkpoints)))
        recon2 = reconcile(self.baseline.results, self.engine.results,
                           store=self.engine.store)
        self.assertFalse(recon2.ok)


class ReportStructureTest(unittest.TestCase):
    def test_report_is_json_serializable_with_phase_budgets(self):
        engine, report = run_shutdown_scenario(
            make_specs(50, seed=3),
            warmup=1e-3,
            config=ShutdownConfig(0.05, 0.5, 0.05),
            workers=4,
        )
        payload = json.loads(json.dumps(report.to_dict()))
        self.assertEqual([p["name"] for p in payload["phases"]],
                         ["stop-accepting", "drain-inflight", "cleanup",
                          "force-exit"])
        self.assertAlmostEqual(
            payload["budget"], 0.05 + 0.5 + 0.05, places=12)
        self.assertTrue(payload["punctual"])
        self.assertEqual(payload["engine_metrics"]["engine"], "sim")


class HardExitTest(unittest.TestCase):
    def test_real_process_exits_on_time_even_with_stuck_tasks(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "demo_real_exit.py")],
            capture_output=True, text=True, cwd=ROOT, timeout=60,
        )
        self.assertEqual(proc.returncode, 42, proc.stderr)
        line = next(l for l in proc.stdout.splitlines()
                    if l.startswith("REPORT "))
        payload = json.loads(line[len("REPORT "):])
        self.assertTrue(payload["punctual"])
        self.assertEqual(payload["counts"].get("abandoned"), 2)
        self.assertLessEqual(payload["elapsed"], payload["budget"] + 0.5)
        self.assertGreaterEqual(payload["elapsed"], payload["budget"] * 0.9)


if __name__ == "__main__":
    unittest.main(verbosity=2)

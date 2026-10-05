"""Self-tests for saga partial commit + idempotent rollback.

Run:  python3 -m unittest test_saga.py -v   (from the saga/ directory)
"""

import json
import random
import unittest
from collections import Counter

from saga import (
    CompensationOutcome,
    Executor,
    KeyNotFound,
    Stage,
    StageState,
    Store,
    TaskState,
)


def boom(msg="boom"):
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# 1. All-success path + visibility rules
# ---------------------------------------------------------------------------

class SuccessAndVisibilityTests(unittest.TestCase):
    def test_all_success_final_state_and_visibility(self):
        store = Store()
        ex = Executor(store)
        external_seen_during = {}

        def s0(ctx):
            ctx.write("a", 1)
            # stage 0 not committed yet: even the task read sees the buffer,
            # but the external reader must see nothing
            with self.assertRaises(KeyNotFound):
                store.read("a", None)

        def s1(ctx):
            self.assertEqual(ctx.read("a"), 1)   # read-your-writes inside task
            ctx.write("b", ctx.read("a") + 1)
            external_seen_during["b"] = None
            with self.assertRaises(KeyNotFound):
                store.read("a", None)            # still invisible after stage commit
            with self.assertRaises(KeyNotFound):
                store.read("b", None)

        def s2(ctx):
            ctx.write("a", 100)                  # overwrite own earlier write

        task_id = "t1"
        result = ex.run(
            [Stage("create_a", s0), Stage("create_b", s1), Stage("update_a", s2)],
            task_id=task_id,
        )

        self.assertEqual(result.state, TaskState.COMMITTED.value)
        self.assertEqual(result.committed_stage_count, 3)
        self.assertIsNone(result.failure_point)
        # only after the whole task commits does the external world see it
        self.assertEqual(store.read("a", None), 100)
        self.assertEqual(store.read("b", None), 2)
        self.assertEqual(
            store.visible_snapshot(None), {"a": 100, "b": 2}
        )
        self.assertEqual(ex.task_state(task_id), TaskState.COMMITTED)

    def test_other_task_cannot_see_uncommitted_stage(self):
        store = Store()
        ex = Executor(store)

        def s0(ctx):
            ctx.write("x", "t2-value")

        # task t2: stage 0 committed, stage 1 performs the cross-task checks
        # and then fails, so t2 rolls back.
        def s1(ctx):
            self.assertEqual(ctx.read("x"), "t2-value")          # own read-your-writes
            with self.assertRaises(KeyNotFound):
                store.read("x", None)                            # external: invisible
            raise RuntimeError("abort")

        result = ex.run([Stage("a", s0), Stage("b", s1)], task_id="t2")
        self.assertEqual(result.state, TaskState.ROLLED_BACK.value)
        # a concurrent baseline writer / external reader never saw t2's value
        with self.assertRaises(KeyNotFound):
            store.read("x", None)

# ---------------------------------------------------------------------------
# 2. First-stage failure: nothing committed, rollback is a no-op
# ---------------------------------------------------------------------------

class FirstStageFailureTests(unittest.TestCase):
    def test_first_stage_failure(self):
        store = Store()
        ex = Executor(store)
        comp_calls = Counter()

        result = ex.run(
            [
                Stage("s0", lambda c: boom("stage0 exploded")),
                Stage("s1", lambda c: c.write("y", 1)),
                Stage("s2", lambda c: c.write("z", 2)),
            ],
            task_id="fail-first",
        )

        self.assertEqual(result.state, TaskState.ROLLED_BACK.value)
        self.assertEqual(result.failure_point, 0)
        self.assertEqual(result.committed_stage_count, 0)
        self.assertEqual(result.rollback, [])
        self.assertEqual(store.visible_snapshot(None), {})
        self.assertEqual(store.total_version_count(), 0)
        self.assertEqual(ex.task_state("fail-first"), TaskState.ROLLED_BACK)


# ---------------------------------------------------------------------------
# 3. Middle-stage failure: prefix committed, then compensated in reverse
# ---------------------------------------------------------------------------

class MiddleStageFailureTests(unittest.TestCase):
    def test_middle_stage_failure_reverse_compensation(self):
        store = Store()
        store.baseline_write("a", "baseline-a")
        ex = Executor(store)
        comp_calls = Counter()

        def comp0(ctx):
            comp_calls["s0"] += 1

        def comp1(ctx):
            comp_calls["s1"] += 1

        result = ex.run(
            [
                Stage("s0", lambda c: c.write("a", "v0"), comp0),
                Stage("s1", lambda c: c.write("b", "v1"), comp1),
                Stage("s2", lambda c: c.write("c", "v2")),
                Stage("s3", lambda c: boom("stage3 exploded")),
            ],
            task_id="fail-mid",
        )

        self.assertEqual(result.failure_point, 3)
        self.assertEqual(result.committed_stage_count, 3)
        self.assertEqual(result.state, TaskState.ROLLED_BACK.value)
        # rollback order: s2 -> s1 -> s0
        self.assertEqual(
            [(r.index, r.state, r.keys_restored) for r in result.rollback],
            [
                (2, StageState.COMPENSATED.name, 1),
                (1, StageState.COMPENSATED.name, 1),
                (0, StageState.COMPENSATED.name, 1),
            ],
        )
        # compensate hooks invoked exactly once, in reverse order
        self.assertEqual(comp_calls, {"s0": 1, "s1": 1})
        # store restored: own versions removed, baseline value visible again
        self.assertEqual(store.read("a", None), "baseline-a")
        with self.assertRaises(KeyNotFound):
            store.read("b", None)
        with self.assertRaises(KeyNotFound):
            store.read("c", None)
        self.assertEqual(
            store.visible_snapshot(None), {"a": "baseline-a"}
        )
        # journal states
        states = [r.state for r in ex.journal("fail-mid")]
        self.assertEqual(
            states,
            [
                StageState.COMPENSATED,
                StageState.COMPENSATED,
                StageState.COMPENSATED,
                StageState.PENDING,
            ],
        )

    def test_rolled_back_versions_were_never_externally_visible(self):
        store = Store()
        ex = Executor(store)
        observed = []

        def s0(ctx):
            ctx.write("k", 1)

        def s1(ctx):
            observed.append(store.read("k", None))  # external view mid-task
            raise RuntimeError("nope")

        result = ex.run([Stage("s0", s0), Stage("s1", s1)], task_id="vis")
        self.assertEqual(result.state, TaskState.ROLLED_BACK.value)
        self.assertEqual(observed, [])
        self.assertEqual(store.visible_snapshot(None), {})


# ---------------------------------------------------------------------------
# 4. Compensation failure mid-rollback + resumable retry
# ---------------------------------------------------------------------------

class CompensationFailureTests(unittest.TestCase):
    def test_compensation_failure_then_retry_succeeds(self):
        store = Store()
        ex = Executor(store)
        comp_calls = Counter()
        flaky = {"fail": True}

        def comp0(ctx):
            comp_calls["s0"] += 1
            if flaky["fail"]:
                raise RuntimeError("compensation network down")

        def comp1(ctx):
            comp_calls["s1"] += 1

        stages = [
            Stage("s0", lambda c: c.write("a", 1), comp0),
            Stage("s1", lambda c: c.write("b", 2), comp1),
            Stage("s2", lambda c: boom("stage2 down")),
        ]
        result = ex.run(stages, task_id="flaky")

        # s1 compensated first (reverse order); s0's compensation fails and
        # rollback stops there; s0's store write is intentionally NOT removed
        self.assertEqual(result.state, TaskState.ROLLBACK_FAILED.value)
        self.assertEqual(
            [(r.index, r.state, r.error is not None) for r in result.rollback],
            [(1, StageState.COMPENSATED.name, False),
             (0, StageState.COMPENSATE_FAILED.name, True)],
        )
        self.assertEqual(ex.task_state("flaky"), TaskState.ROLLBACK_FAILED)
        journal = {r.index: r for r in ex.journal("flaky")}
        self.assertEqual(journal[1].state, StageState.COMPENSATED)
        self.assertEqual(journal[0].state, StageState.COMPENSATE_FAILED)
        # s0 write still present internally, invisible externally (task not committed)
        with self.assertRaises(KeyNotFound):
            store.read("a", None)
        with self.assertRaises(KeyNotFound):
            store.read("b", None)

        # repair the fault and retry rollback: only s0 retried
        flaky["fail"] = False
        retry = ex.rollback("flaky", stages)
        self.assertEqual(ex.task_state("flaky"), TaskState.ROLLED_BACK)
        self.assertEqual(
            [(r.index, r.state) for r in retry],
            [(1, StageState.COMPENSATED.name),   # s1 reported as already done
             (0, StageState.COMPENSATED.name)],  # s0 retried -> success
        )
        # s1 hook must NOT have been re-invoked; s1 keys_restored == 0 on retry
        self.assertEqual(comp_calls, {"s0": 2, "s1": 1})
        s1_retry = retry[0]
        self.assertEqual(s1_retry.keys_restored, 0)
        self.assertEqual(store.visible_snapshot(None), {})


# ---------------------------------------------------------------------------
# 5. Idempotent rollback: repeated rollback has zero additional side effects
# ---------------------------------------------------------------------------

class IdempotentRollbackTests(unittest.TestCase):
    def test_repeated_rollback_no_extra_side_effects(self):
        store = Store()
        ex = Executor(store)
        comp_calls = Counter()

        def make_comp(name):
            def comp(ctx):
                comp_calls[name] += 1
            return comp

        stages = [
            Stage("s0", lambda c: c.write("a", 1), make_comp("s0")),
            Stage("s1", lambda c: c.write("b", 2), make_comp("s1")),
            Stage("s2", lambda c: boom("x")),
        ]
        first = ex.run(stages, task_id="idem")
        self.assertEqual(first.state, TaskState.ROLLED_BACK.value)

        snapshots = [store.visible_snapshot(None)]
        version_counts = [store.total_version_count()]
        repeats = [ex.rollback("idem", stages) for _ in range(3)]

        # no compensate hook is ever invoked again
        self.assertEqual(comp_calls, {"s0": 1, "s1": 1})
        # store state identical across every repeated rollback
        for _ in repeats:
            snapshots.append(store.visible_snapshot(None))
            version_counts.append(store.total_version_count())
        self.assertEqual(len(set(map(json.dumps, snapshots))), 1)
        self.assertEqual(len(set(version_counts)), 1)
        # every repeated rollback reports stages already COMPENSATED, 0 restores
        for rep in repeats:
            self.assertEqual(
                [(r.index, r.state, r.keys_restored) for r in rep],
                [
                    (1, StageState.COMPENSATED.name, 0),
                    (0, StageState.COMPENSATED.name, 0),
                ],
            )
        self.assertEqual(ex.task_state("idem"), TaskState.ROLLED_BACK)

    def test_rollback_cannot_delete_later_writers_values(self):
        # version-tag gating: if another (committed) task later writes the same
        # key, re-running rollback must not delete that value
        store = Store()
        ex = Executor(store)

        def fail(ctx):
            raise RuntimeError("x")

        r1 = ex.run(
            [Stage("a", lambda c: c.write("k", "old")), Stage("b", fail)],
            task_id="tA",
        )
        self.assertEqual(r1.state, TaskState.ROLLED_BACK.value)
        r2 = ex.run([Stage("a", lambda c: c.write("k", "new"))], task_id="tB")
        self.assertEqual(r2.state, TaskState.COMMITTED.value)
        # repeated rollback of tA is harmless
        ex.rollback("tA", [Stage("a", lambda c: None), Stage("b", fail)])
        self.assertEqual(store.read("k", None), "new")


# ---------------------------------------------------------------------------
# 6. Differential test: staged final state == non-staged final state
# ---------------------------------------------------------------------------

class DifferentialTests(unittest.TestCase):
    def test_staged_vs_plain_matches(self):
        rng = random.Random(20261005)
        n_tasks = 300
        n_stages = 5
        staged_store = Store()
        plain_store = Store()
        mismatches = 0

        for t in range(n_tasks):
            keys = [f"k{rng.randrange(40)}" for _ in range(rng.randrange(1, n_stages))]
            staged = []
            for s, key in enumerate(keys):
                value = f"t{t}-s{s}-v{rng.randrange(1000)}"
                staged.append(
                    Stage(f"s{s}", (lambda k, v: lambda c: c.write(k, v))(key, value))
                )
                # reference path: same writes, applied with no staging at all
                plain_store.baseline_write(key, value)
            result = Executor(staged_store).run(staged, task_id=f"task-{t}")
            self.assertEqual(result.state, TaskState.COMMITTED.value)

        left = staged_store.visible_snapshot(None)
        right = plain_store.visible_snapshot(None)
        self.assertEqual(left, right, "staged and non-staged final states differ")
        self.assertGreater(len(left), 0)

    def test_overwrite_chain_matches(self):
        # same key written in every stage: final value is the last write,
        # with no leftover versions externally observable
        staged = Store()
        plain = Store()
        Executor(staged).run(
            [Stage(str(i), (lambda v: lambda c: c.write("k", v))(v))
             for i, v in enumerate([1, 2, 3, 4])],
            task_id="chain",
        )
        for v in [1, 2, 3, 4]:
            plain.baseline_write("k", v)
        self.assertEqual(staged.visible_snapshot(None), plain.visible_snapshot(None))
        self.assertEqual(staged.visible_snapshot(None), {"k": 4})


if __name__ == "__main__":
    unittest.main(verbosity=2)

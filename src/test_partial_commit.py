"""partial_commit 自测（unittest，仅标准库）。

覆盖：全部成功 / 第一阶段失败 / 中间阶段失败 / 回滚中再次失败 /
重复回退幂等 / 成功路径与非阶段化执行的对拍（含随机用例）。
"""

import random
import unittest

from partial_commit import (
    MISSING,
    Stage,
    Store,
    Task,
    Transaction,
)


class VisibilityTests(unittest.TestCase):
    """规则 V1/V2/V4 的可见性测试。"""

    def test_uncommitted_writes_invisible_outside_task(self) -> None:
        store = Store()
        store._apply("k0", "v0")

        tx = Transaction(store)
        tx.write("k1", "private")
        tx.write("k0", "private-override")

        # V1: 任务内 read-your-writes
        self.assertEqual(tx.read("k1"), "private")
        self.assertEqual(tx.read("k0"), "private-override")
        # V1: 外部读者完全看不到未提交写入
        self.assertEqual(store.read("k0"), "v0")
        self.assertIsNone(store.read("k1"))
        self.assertEqual(store.snapshot(), {"k0": "v0"})

    def test_committed_stage_visible_and_failed_stage_dropped(self) -> None:
        store = Store()
        called = []

        def s0(tx: Transaction) -> None:
            tx.write("a", 1)

        def s1(tx: Transaction) -> None:
            tx.write("b", 2)  # 提交后进入下一阶段时对外可见

        def s2(tx: Transaction) -> None:
            called.append(store.snapshot())
            tx.write("c", 3)
            raise RuntimeError("boom")  # 同阶段未提交写入必须丢弃（V4）

        task = Task(store, [Stage("s0", s0), Stage("s1", s1), Stage("s2", s2)])
        result = task.run()

        # 进入阶段2时 s0/s1 已部分提交，外部看到 a,b；阶段2的 c 从未对外可见
        self.assertEqual(called[0], {"a": 1, "b": 2})
        # 回滚后外部回到任务前状态（V3），且 c 从未出现
        self.assertEqual(store.snapshot(), {})
        self.assertEqual(result.status, "rolled_back")
        self.assertEqual(result.failed_stage, 2)


class SuccessTests(unittest.TestCase):
    def test_all_stages_success(self) -> None:
        store = Store()
        stages = [
            Stage("s0", lambda tx: tx.write("a", 1)),
            Stage("s1", lambda tx: tx.write("b", tx.read("a") + 1)),
            Stage("s2", lambda tx: tx.write("c", 3)),
        ]
        result = Task(store, stages).run()

        self.assertEqual(result.status, "committed")
        self.assertEqual(store.snapshot(), {"a": 1, "b": 2, "c": 3})
        self.assertIsNone(result.rollback)
        self.assertEqual(store.commit_count, 3)

    def test_partial_commit_visibility_progression(self) -> None:
        """V2：每个阶段提交点之后，外部可见状态逐步推进。"""
        store = Store()
        seen = []
        stages = [
            Stage("s0", lambda tx: tx.write("a", 1)),
            Stage("s1", lambda tx: (seen.append(store.snapshot()), tx.write("b", 2))[1]),
            Stage("s2", lambda tx: (seen.append(store.snapshot()), tx.write("c", 3))[1]),
        ]
        Task(store, stages).run()
        seen.append(store.snapshot())
        self.assertEqual(seen, [{"a": 1}, {"a": 1, "b": 2}, {"a": 1, "b": 2, "c": 3}])


class FailureTests(unittest.TestCase):
    def test_first_stage_failure_nothing_committed(self) -> None:
        store = Store()
        store._apply("seed", 0)

        def bad(tx: Transaction) -> None:
            tx.write("a", 1)
            raise ValueError("first stage down")

        result = Task(store, [Stage("s0", bad), Stage("s1", lambda tx: tx.write("b", 2))]).run()

        self.assertEqual(result.status, "rolled_back")
        self.assertEqual(result.failed_stage, 0)
        # 没有任何阶段提交过：存储不变、无回滚条目
        self.assertEqual(store.snapshot(), {"seed": 0})
        self.assertEqual(result.rollback.per_stage, [])
        self.assertTrue(result.rollback.fully_rolled_back)
        self.assertEqual(store.commit_count, 0)

    def test_middle_stage_failure_rolls_back_committed_stages(self) -> None:
        store = Store()
        store._apply("seed", 9)

        stages = [
            Stage("s0", lambda tx: tx.write("a", 1)),
            Stage("s1", lambda tx: (tx.write("b", 2), tx.write("seed", 99))),
            Stage("s2", lambda tx: (_ for _ in ()).throw(RuntimeError("mid boom"))),
        ]
        result = Task(store, stages).run()

        self.assertEqual(result.failed_stage, 2)
        self.assertEqual(result.status, "rolled_back")
        # 已提交阶段 s0/s1 的效果全部撤销（包括对已有键 seed 的改值）
        self.assertEqual(store.snapshot(), {"seed": 9})
        rb = result.rollback
        self.assertTrue(rb.fully_rolled_back)
        self.assertEqual([r.stage for r in rb.per_stage], ["s1", "s0"])  # 逆序回滚
        self.assertTrue(all(r.state == "done" and r.attempts == 1 for r in rb.per_stage))

    def test_delete_rollback_when_key_did_not_exist(self) -> None:
        store = Store()
        stages = [
            Stage("s0", lambda tx: tx.write("new", 1)),
            Stage("s1", lambda tx: (_ for _ in ()).throw(RuntimeError("x"))),
        ]
        Task(store, stages).run()
        self.assertEqual(store.snapshot(), {})  # 新键回滚时删除，而非残留

    def test_delete_of_existing_key_rolled_back(self) -> None:
        store = Store()
        store._apply("keep", 7)
        stages = [
            Stage("s0", lambda tx: tx.delete("keep")),
            Stage("s1", lambda tx: (_ for _ in ()).throw(RuntimeError("x"))),
        ]
        Task(store, stages).run()
        self.assertEqual(store.snapshot(), {"keep": 7})  # 删除被撤销，旧值恢复


class IdempotentRollbackTests(unittest.TestCase):
    def test_repeated_rollback_no_extra_side_effects(self) -> None:
        store = Store()
        task = Task(store, [
            Stage("s0", lambda tx: tx.write("a", 1)),
            Stage("s1", lambda tx: tx.write("b", 2)),
            Stage("s2", lambda tx: (_ for _ in ()).throw(RuntimeError("boom"))),
        ])
        result = task.run()
        self.assertEqual(result.status, "rolled_back")

        writes_after_first = store.write_count  # run() 内已自动回滚（第 1 次）
        rb1 = result.rollback
        rb2 = task.rollback()
        rb3 = task.rollback()

        # 每次回滚结果一致；每个阶段补偿只实际执行过 1 次
        for r in rb2.per_stage + rb3.per_stage:
            self.assertEqual(r.state, "done")
            self.assertEqual(r.attempts, 1)
        # 重复回退不产生额外存储写
        self.assertEqual(store.write_count, writes_after_first)
        self.assertEqual(rb1.rollback_calls, 1)
        self.assertEqual(rb3.rollback_calls, 3)
        self.assertEqual(store.snapshot(), {})


class RollbackFailureTests(unittest.TestCase):
    """回滚中再次失败的情形。"""

    @staticmethod
    def _undo_entries(store: Store, entries) -> None:
        for key, old in reversed(entries):
            if old is MISSING:
                store._delete(key)
            else:
                store._apply(key, old)

    def test_compensator_fails_once_then_succeeds_on_retry(self) -> None:
        store = Store()
        store._apply("a", 0)
        attempts = {"bad": 0, "good": 0}

        def bad_compensate(s: Store, entries) -> None:
            attempts["bad"] += 1
            if attempts["bad"] == 1:
                raise RuntimeError("compensation crashed")
            self._undo_entries(s, entries)  # 第二次成功：正常撤销

        def good_compensate(s: Store, entries) -> None:
            attempts["good"] += 1
            self._undo_entries(s, entries)

        stages = [
            Stage("s0", lambda tx: tx.write("a", 1), compensate=good_compensate),
            Stage("s1", lambda tx: tx.write("b", 2), compensate=bad_compensate),
            Stage("s2", lambda tx: (_ for _ in ()).throw(RuntimeError("task boom"))),
        ]
        task = Task(store, stages)
        r1 = task.run()

        # 第一次回滚：s1 补偿失败，s0 仍被回滚（失败不阻断其他阶段）
        self.assertEqual(r1.status, "rollback_incomplete")
        by = {r.stage: r for r in r1.rollback.per_stage}
        self.assertEqual(by["s1"].state, "failed")
        self.assertEqual(by["s1"].attempts, 1)
        self.assertIn("compensation crashed", by["s1"].error)
        self.assertEqual(by["s0"].state, "done")
        self.assertEqual(store.snapshot(), {"a": 0, "b": 2})  # s0 已撤销，s1 未撤销

        r2 = task.rollback()

        # 第二次回滚：只重试 FAILED 的 s1；DONE 的 s0 不重做（幂等）
        self.assertTrue(r2.fully_rolled_back)
        by2 = {r.stage: r for r in r2.per_stage}
        self.assertEqual(by2["s1"].state, "done")
        self.assertEqual(by2["s1"].attempts, 2)
        self.assertEqual(by2["s0"].attempts, 1)  # s0 没有第二次副作用
        self.assertEqual(attempts["good"], 1)
        self.assertEqual(store.snapshot(), {"a": 0})
        self.assertIsNone(store.read("b"))

        # 再回滚一次：全部 DONE，零额外副作用
        writes_after_retry = store.write_count
        r3 = task.rollback()
        self.assertTrue(r3.fully_rolled_back)
        self.assertEqual(store.write_count, writes_after_retry)
        self.assertEqual(attempts["good"], 1)
        self.assertEqual(attempts["bad"], 2)

    def test_compensator_fails_permanently(self) -> None:
        store = Store()
        side_effects = [0]

        def always_fail(s: Store, entries) -> None:
            side_effects[0] += 1
            raise RuntimeError("permanent")

        stages = [
            Stage("s0", lambda tx: tx.write("a", 1)),
            Stage("s1", lambda tx: tx.write("b", 2), compensate=always_fail),
            Stage("s2", lambda tx: (_ for _ in ()).throw(RuntimeError("boom"))),
        ]
        task = Task(store, stages)
        r1 = task.run()
        self.assertFalse(r1.rollback.fully_rolled_back)
        # s0 已撤销；s1 永久失败，其效果残留（结果中显式报告 failed）
        self.assertEqual(store.snapshot(), {"b": 2})
        r2 = task.rollback()
        r3 = task.rollback()
        self.assertFalse(r2.fully_rolled_back)
        self.assertFalse(r3.fully_rolled_back)
        # s0 的默认撤销只执行一次；失败的 s1 每次调用被重试 1 次
        by = {r.stage: r for r in r3.per_stage}
        self.assertEqual(by["s0"].attempts, 1)
        self.assertEqual(by["s1"].attempts, 3)
        self.assertEqual(side_effects[0], 3)


class EquivalenceTests(unittest.TestCase):
    """成功路径对拍：阶段化执行的最终状态 == 非阶段化（直接顺序执行）。"""

    def test_equivalence_simple(self) -> None:
        staged_store = Store()
        plain_store = Store()

        ops = [("set", "a", 1), ("set", "a", 11), ("set", "b", 22),
               ("del", "a"), ("set", "c", 33)]

        def make_apply(op):
            if op[0] == "set":
                _, k, v = op
                return lambda tx: tx.write(k, v)
            k = op[1]
            return lambda tx: tx.delete(k)

        stages = [Stage(f"s{i}", make_apply(op)) for i, op in enumerate(ops)]
        r = Task(staged_store, stages).run()
        self.assertEqual(r.status, "committed")

        # 非阶段化：同样的操作直接写
        for op in ops:
            if op[0] == "set":
                plain_store._apply(op[1], op[2])
            else:
                plain_store._delete(op[1])
        self.assertEqual(staged_store.snapshot(), plain_store.snapshot())

    def test_equivalence_random_diff(self) -> None:
        """随机对拍：N 个随机任务，阶段化 vs 一次性直接写，最终状态必须一致。"""
        rng = random.Random(20261005)
        N = 2000
        mismatches = 0
        sample = None
        for _ in range(N):
            n_stages = rng.randint(1, 6)
            staged_store = Store()
            plain_store = Store()
            task_ops = []
            for _ in range(n_stages):
                batch = {f"k{rng.randrange(20)}": rng.randrange(1000)
                         for _ in range(rng.randint(1, 4))}
                task_ops.append(batch)

            def make_apply(batch):
                def apply(tx):
                    for k, v in batch.items():
                        tx.write(k, v)
                return apply

            stages = [Stage(f"s{i}", make_apply(b)) for i, b in enumerate(task_ops)]
            r = Task(staged_store, stages).run()
            self.assertEqual(r.status, "committed")
            for b in task_ops:
                for k, v in b.items():
                    plain_store._apply(k, v)
            if staged_store.snapshot() != plain_store.snapshot():
                mismatches += 1
                sample = task_ops
        self.assertEqual(mismatches, 0, f"对拍不一致示例: {sample}")

    def test_rollback_equivalence_with_never_executed(self) -> None:
        """失败路径对拍：回滚后的状态 == 任务从未执行的状态。"""
        rng = random.Random(42)
        for _ in range(500):
            initial = {f"k{rng.randrange(10)}": rng.randrange(100)
                       for _ in range(rng.randint(0, 5))}
            store = Store()
            for k, v in initial.items():
                store._apply(k, v)
            before = store.snapshot()
            fail_at = rng.randrange(4)

            def make_apply(idx):
                def apply(tx):
                    if idx == fail_at:
                        raise RuntimeError(f"fail at {idx}")
                    tx.write(f"x{idx}", idx)
                return apply

            stages = [Stage(f"s{i}", make_apply(i)) for i in range(4)]
            r = Task(store, stages).run()
            self.assertEqual(r.status, "rolled_back")
            self.assertEqual(store.snapshot(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""自测套件（unittest，零依赖）。

覆盖：
  T1 无冲突（SAT）：状态与赋值证据都正确；
  T2 单一最小冲突：MUS 正确、环外冗余约束不进 MUS；
  T3 多个最小冲突：分支枚举结果与暴力真值机逐一一致；
  T4 约束自相矛盾：x-x<=c(c<0) 单条即 MUS，且与"无冲突"分开报告；
  T5 随机性质测试：枚举 MUS 集合 == 暴力枚举；每个 MUS 逐条删除验证；
     删除过滤法与负环法提取的 MUS 同为极小；
  T6 平行边/加弦不破坏最小性。

运行：python3 -m unittest tests.test_conflicts -v
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conflict_locator import (
    Constraint, DifferenceSystem, brute_force_mus, diagnose,
    enumerate_mus, extract_mus_deletion, find_one_mus, find_some_mus,
    render_report, verify_mus,
)


def C(u, v, c, cid=""):
    return Constraint(u, v, c, cid)


def assignment_satisfies(cons, assignment):
    return all(assignment[c.u] - assignment[c.v] <= c.c for c in cons)


class T1NoConflict(unittest.TestCase):
    def test_sat_report(self):
        # x2 - x1 <= 5; x3 - x2 <= 2; x1 - x3 <= 0 （总权 7 > 0，可满足）
        cons = [C(2, 1, 5), C(3, 2, 2), C(1, 3, 0)]
        d = diagnose(cons)
        self.assertEqual(d.status, "SAT")
        self.assertEqual(d.conflicts, [])
        self.assertTrue(assignment_satisfies(cons, d.assignment))
        self.assertEqual(render_report(d).splitlines()[0], "=" * 56)

    def test_empty_system(self):
        d = diagnose([])
        self.assertEqual(d.status, "SAT")
        self.assertEqual(d.n_constraints, 0)


class T2SingleMinimalConflict(unittest.TestCase):
    def test_single_mus_with_redundant_constraints(self):
        # 负环: x1-x2<=-1 (C1), x2-x1<=0 (C2)；C3 为环外无关约束
        cons = [C(1, 2, -1, "a"), C(2, 1, 0, "b"),
                C(3, 1, 5, "extra")]
        d = diagnose(cons, max_mus=10)
        self.assertEqual(d.status, "UNSAT")
        self.assertEqual(len(d.conflicts), 1)
        mc = d.conflicts[0]
        self.assertEqual(set(mc.cids), {"a", "b"})
        self.assertTrue(mc.verified_minimal)
        self.assertEqual(len(mc.verification), 2)
        self.assertTrue(all(ok for _, ok in mc.verification))
        # 环外约束不在 MUS 中
        self.assertNotIn("extra", mc.cids)

    def test_added_chord_not_in_mus(self):
        # 3 边负环 total -1；额外加一条平行弦不会扩大 MUS（环保持简单极小）
        cons = [C(1, 3, 0), C(2, 1, 0), C(3, 2, -1), C(1, 3, 100)]
        mc = find_one_mus(cons)
        self.assertEqual(mc.size, 3)
        self.assertTrue(mc.verified_minimal)


class T3MultipleMinimalConflicts(unittest.TestCase):
    def test_two_disjoint_cycles(self):
        # 环 A: a1,a2；环 B: b1,b2
        cons = [C(1, 2, -1, "a1"), C(2, 1, 0, "a2"),
                C(3, 4, -1, "b1"), C(4, 3, 0, "b2"),
                C(1, 3, 7, "bridge")]
        d = diagnose(cons, max_mus=10)
        self.assertEqual(d.status, "UNSAT")
        self.assertTrue(d.enumeration_complete)
        cid_sets = {frozenset(mc.cids) for mc in d.conflicts}
        self.assertEqual(cid_sets,
                         {frozenset({"a1", "a2"}), frozenset({"b1", "b2"})})
        for mc in d.conflicts:
            self.assertTrue(mc.verified_minimal)

    def test_overlapping_cycles(self):
        # 两个共享一条边的负环：
        # 边: 1->2(-10) 2->1(0) [环A]; 1->3(0) 3->2(0) 与 1->2?
        # 构造: C1: x2-x1<=-10 (1->2,-10), C2: x1-x2<=0 (2->1,0)
        #       C3: x3-x2<=0 (2->3,0), C4: x2-x3<=9 (3->2,9)
        # 环B = 1->2(-10), 2->3(0), 3->2? 不对。改成：
        # C3: x3-x1<=0 (1->3,0), C4: x1-x3<=9 (3->1,9)
        # 环B: 1->2(-10),2->1? 不共享。用: 1->2(-10),2->3(0)? 无 2->3 边。
        # 清晰构造共享边 1->2(-10):
        #  环A: 1->2(-10), 2->1(0)
        #  环B: 1->2(-10), 2->3(0), 3->1(9)   total -1
        cons = [C(2, 1, -10, "e12"), C(1, 2, 0, "e21"),
                C(3, 2, 0, "e23"), C(1, 3, 9, "e31")]
        truth = brute_force_mus(cons)
        found, complete, _ = enumerate_mus(cons, max_mus=50)
        got = {frozenset(mc.cids) for mc in found}
        self.assertTrue(complete)
        self.assertEqual(got, set(truth))
        self.assertEqual(len(truth), 2)
        self.assertTrue(all(mc.verified_minimal for mc in found))


class T4SelfContradictory(unittest.TestCase):
    def test_single_self_contradiction(self):
        cons = [C(1, 1, -5, "bad")]
        d = diagnose(cons)
        self.assertEqual(d.status, "UNSAT")           # 不是 SAT
        self.assertEqual(len(d.conflicts), 1)
        mc = d.conflicts[0]
        self.assertEqual(mc.size, 1)
        self.assertTrue(mc.self_contradictory)
        self.assertEqual(mc.cids, ("bad",))
        self.assertIn("bad", d.self_contradictory_cids)

    def test_mixed_with_sat_constraints(self):
        # 其余全部可满足，仅一条自相矛盾
        cons = [C(2, 1, 3, "ok1"), C(3, 2, 2, "ok2"),
                C(2, 2, -1, "bad")]
        d = diagnose(cons)
        self.assertEqual(d.status, "UNSAT")
        self.assertEqual({frozenset(mc.cids) for mc in d.conflicts},
                         {frozenset({"bad"})})

    def test_zero_weight_self_loop_is_ok(self):
        # x - x <= 0 不是矛盾
        d = diagnose([C(1, 1, 0, "zero")])
        self.assertEqual(d.status, "SAT")


class T5RandomPropertyTests(unittest.TestCase):
    SEED = 20261007
    N_TRIALS = 60

    def test_random_against_brute_force(self):
        rng = random.Random(self.SEED)
        n_sat = n_unsat = 0
        for trial in range(self.N_TRIALS):
            n_vars = rng.randint(3, 6)
            m = rng.randint(1, 8)
            cons = []
            for j in range(m):
                u = rng.randint(1, n_vars)
                v = rng.randint(1, n_vars)
                c = rng.randint(-6, 6)
                cons.append(C(u, v, c, f"t{trial}_{j}"))
            truth = brute_force_mus(cons)
            found, complete, calls = enumerate_mus(cons, max_mus=100,
                                                   max_calls=10000)
            self.assertTrue(complete, f"trial {trial}: 枚举不完整")
            got = {frozenset(mc.cids) for mc in found}
            self.assertEqual(got, set(truth),
                             f'trial {trial}: MUS 集合不一致 {cons}')
            for mc in found:
                self.assertTrue(mc.verified_minimal)
            # 状态二分
            d = diagnose(cons, max_mus=100, max_calls=10000)
            if truth:
                n_unsat += 1
                self.assertEqual(d.status, "UNSAT")
            else:
                n_sat += 1
                self.assertEqual(d.status, "SAT")
                self.assertTrue(assignment_satisfies(cons, d.assignment))
        # 随机数据中两类都应出现（否则测试覆盖不足）
        self.assertGreater(n_sat, 10)
        self.assertGreater(n_unsat, 10)

    def test_deletion_filter_agrees(self):
        rng = random.Random(self.SEED + 1)
        for _ in range(30):
            n_vars = rng.randint(3, 6)
            cons = [C(rng.randint(1, n_vars), rng.randint(1, n_vars),
                      rng.randint(-6, 6)) for _ in range(rng.randint(2, 8))]
            d_del = extract_mus_deletion(cons)
            d_cyc = find_one_mus(cons)
            if d_del is None:
                self.assertIsNone(d_cyc)
                continue
            self.assertIsNotNone(d_cyc)
            # 两者都是 MUS（不要求相同：多 MUS 时算法可任选）
            self.assertTrue(d_del.verified_minimal)
            self.assertTrue(d_cyc.verified_minimal)
            truth = brute_force_mus(cons)
            self.assertIn(frozenset(d_del.cids), truth)
            self.assertIn(frozenset(d_cyc.cids), truth)


class T6ParallelEdges(unittest.TestCase):
    def test_parallel_edges_min_weight(self):
        # (u,v) 两条平行边，较松的一条不进 MUS
        # 1->2: -1 与 100；2->1: 0。唯一负环用 -1 那条
        cons = [C(2, 1, 100, "loose"), C(2, 1, -1, "tight"),
                C(1, 2, 0, "back")]
        truth = brute_force_mus(cons)
        found, complete, _ = enumerate_mus(cons)
        self.assertTrue(complete)
        got = {frozenset(mc.cids) for mc in found}
        self.assertEqual(got, set(truth))
        self.assertEqual(got, {frozenset({"tight", "back"})})


class T7ScaleSanity(unittest.TestCase):
    def test_many_disjoint_cycles(self):
        # 200 个互不相交的 2 环（400 变量），只取前 3 个 MUS；
        # 显式栈枚举在深分支下不应受递归深度限制。
        cons = []
        for k in range(200):
            a, b = 2 * k + 1, 2 * k + 2
            cons.append(C(a, b, -1, f"p{k}"))
            cons.append(C(b, a, 0, f"q{k}"))
        found, complete, calls = enumerate_mus(cons, max_mus=3, max_calls=10000)
        self.assertEqual(len(found), 3)
        self.assertEqual(len({frozenset(mc.cids) for mc in found}), 3)
        for mc in found:
            self.assertTrue(mc.verified_minimal)
            self.assertEqual(mc.size, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

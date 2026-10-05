# -*- coding: utf-8 -*-
"""自测:四情形 + 随机对拍(快速实现 vs 逐对试探参考实现)。

运行:python3 tests.py
"""

import random
import sys
import unittest

from fsm import (
    FSM, MISSING, analyze, distinguishing_sequence, equivalence_evidence,
    minimize, partition_is_stable, reference_classes, same_partition,
    simulate,
)
import scenarios


def random_machine(rng, n_states, n_inputs, n_outputs, missing_prob=0.0,
                   extra_states=0):
    """随机 Mealy 机;extra_states 个状态不接边,用于制造不可达状态。"""
    n = n_states + extra_states
    states = ["s%d" % i for i in range(n)]
    inputs = ["x%d" % i for i in range(n_inputs)]
    trans, out = {}, {}
    for s in states[:n_states]:
        for x in inputs:
            if rng.random() < missing_prob:
                continue
            trans[(s, x)] = states[rng.randrange(n_states)]
            out[(s, x)] = "o%d" % rng.randrange(n_outputs)
    return FSM(states, inputs, states[0], trans, out)


class ScenarioTests(unittest.TestCase):
    def test_all_distinguishable(self):
        r = analyze(scenarios.all_distinguishable())
        self.assertEqual(r["class_count"], 4)
        self.assertTrue(all(c["size"] == 1 for c in r["classes"]))
        self.assertEqual(r["unreachable"], [])
        self.assertEqual(r["missing_transitions"], [])
        self.assertTrue(r["stable"])

    def test_with_equivalence(self):
        r = analyze(scenarios.with_equivalence())
        classes = sorted(sorted(c["states"]) for c in r["classes"])
        self.assertEqual(classes, [["A", "D"], ["B", "C"]])
        self.assertTrue(r["stable"])
        # 等价对的论证数据必须逐项一致
        for c in r["classes"]:
            ev = c["evidence_example"]
            self.assertTrue(ev["consistent"])

    def test_unreachable_separate(self):
        r = analyze(scenarios.with_unreachable())
        self.assertEqual(sorted(r["unreachable"]), ["G0", "G1"])
        in_classes = sorted(s for c in r["classes"] for s in c["states"])
        self.assertEqual(in_classes, ["M0", "M1"])
        self.assertNotIn("G0", in_classes)

    def test_missing_separate(self):
        r = analyze(scenarios.with_missing())
        kinds = {(s, x): k for s, x, k in r["missing_transitions"]}
        self.assertEqual(kinds, {("P", "b"): "both", ("R", "a"): "both"})
        # 缺失转移不得出现在等价类报告中充当"状态"
        in_classes = sorted(s for c in r["classes"] for s in c["states"])
        self.assertEqual(in_classes, ["P", "Q", "R"])

    def test_missing_semantics(self):
        # 补全语义:缺失输出 ⊥ 与真实输出不同,可区分
        m = FSM(["u", "v"], ["a"], "u",
                {("u", "a"): "u", ("v", "a"): "v"},
                {("u", "a"): 0})  # (v,a) 输出缺失
        w = distinguishing_sequence(m, "u", "v")
        self.assertIsNotNone(w)
        self.assertEqual(w["outputs_q"], [MISSING])


class CrossCheckTests(unittest.TestCase):
    """对拍:Hopcroft 划分 vs 逐对试探参考实现,外加逐对证据校验。"""

    def check_one(self, machine, tag):
        reachable = machine.reachable_states()
        blocks, block_of = minimize(machine, reachable)
        rblocks, rblock_of = reference_classes(machine, reachable)
        self.assertTrue(
            same_partition(blocks, block_of, rblocks, rblock_of),
            "%s: 快速实现与参考实现划分不一致" % tag)
        self.assertTrue(
            partition_is_stable(machine, blocks, block_of),
            "%s: 划分不稳定" % tag)

        states = sorted(reachable, key=repr)
        for i in range(len(states)):
            for j in range(i + 1, len(states)):
                p, q = states[i], states[j]
                if block_of[p] == block_of[q]:
                    # 同块:参考实现也必须找不到区分序列
                    self.assertIsNone(
                        distinguishing_sequence(machine, p, q),
                        "%s: 同块对 (%r,%r) 却找到区分序列" % (tag, p, q))
                    ev = equivalence_evidence(machine, p, q, block_of)
                    self.assertTrue(ev["consistent"])
                else:
                    # 跨块:必须能给出真实可区分的输入序列
                    w = distinguishing_sequence(machine, p, q)
                    self.assertIsNotNone(
                        w, "%s: 跨块对 (%r,%r) 找不到区分序列" % (tag, p, q))
                    self.assertNotEqual(w["outputs_p"], w["outputs_q"])
                    self.assertEqual(w["outputs_p"],
                                     simulate(machine, p, w["sequence"]))
                    self.assertEqual(w["outputs_q"],
                                     simulate(machine, q, w["sequence"]))

    def test_random_small(self):
        rng = random.Random(20261005)
        cases = 0
        for trial in range(300):
            n = rng.randint(2, 12)
            ni = rng.randint(1, 3)
            no = rng.randint(1, 3)
            mp = rng.choice([0.0, 0.1, 0.3])
            extra = rng.choice([0, 0, 2])
            m = random_machine(rng, n, ni, no, mp, extra)
            self.check_one(m, "random#%d n=%d" % (trial, n))
            cases += 1
        print("对拍通过: %d 台随机小机(含缺失转移/不可达状态)" % cases,
              file=sys.stderr)

    def test_random_medium(self):
        rng = random.Random(7)
        for trial in range(20):
            m = random_machine(rng, 60, 3, 2, missing_prob=0.05)
            self.check_one(m, "medium#%d" % trial)
        print("对拍通过: 20 台 60 状态随机机", file=sys.stderr)

    def test_forced_equivalence(self):
        # 两台同构副本拼在一起,强制产生大等价类
        rng = random.Random(99)
        base = random_machine(rng, 30, 2, 2)
        states = ["L_" + s for s in base.states] + ["R_" + s for s in base.states]
        trans, out = {}, {}
        for (s, x), t in base.trans.items():
            trans[("L_" + s, x)] = "L_" + t
            trans[("R_" + s, x)] = "R_" + t
        for (s, x), o in base.out.items():
            out[("L_" + s, x)] = o
            out[("R_" + s, x)] = o
        m = FSM(states, base.inputs, "L_" + base.initial, trans, out)
        reachable = m.reachable_states()
        base_reachable = base.reachable_states()
        blocks, block_of = minimize(m, reachable)
        rblocks, rblock_of = reference_classes(m, reachable)
        self.assertTrue(same_partition(blocks, block_of, rblocks, rblock_of))
        # 右半不可达;左半内部若有等价类须与参考一致(上面已校验)
        self.assertEqual(len(reachable), len(base_reachable))
        self.assertFalse(any(s.startswith('R_') for s in reachable))
        print("对拍通过: 同构副本机(强制等价结构)", file=sys.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

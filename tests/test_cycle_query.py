"""cycle_query 自测：手工对拍 + 暴力 oracle 对拍 + 边界用例 + 深链。"""

import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from cycle_query import (
    Cycle,
    Graph,
    QueryResult,
    Truncation,
    find_all_cycles,
    recursive_query,
)
from oracle import oracle_cycles

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "crosscheck_cases.json")


def load_cases():
    with open(DATA, encoding="utf-8") as f:
        return json.load(f)


def encode_rows(rows):
    """把 rows 编码成对拍数据里的标记形式。"""
    out = []
    for r in rows:
        if isinstance(r, Cycle):
            out.append("CYCLE[" + ",".join(str(x) for x in r.nodes) + "]")
        elif isinstance(r, Truncation):
            out.append("TRUNC(%s,%s)" % (r.node, r.at_depth))
        else:
            out.append(r)
    return out


class CrossCheckTest(unittest.TestCase):
    """库输出 vs 手工推导数据（data/crosscheck_cases.json）。"""

    @classmethod
    def setUpClass(cls):
        cls.spec = load_cases()
        cls.graph = Graph([tuple(e) for e in cls.spec["graph"]["edges"]])

    def test_handcrafted_cases(self):
        for case in self.spec["cases"]:
            with self.subTest(case=case["name"]):
                roots = case["roots"]
                res = recursive_query(
                    self.graph, roots if len(roots) > 1 else roots[0],
                    max_depth=case["max_depth"],
                )
                exp = case["expected"]
                self.assertEqual(res.shape, exp["shape"])
                self.assertEqual(encode_rows(res.rows), exp["rows"])
                self.assertEqual([list(c.nodes) for c in res.cycles], exp["cycles"])
                self.assertEqual(
                    [{"node": t.node, "depth": t.at_depth} for t in res.truncated],
                    exp["truncated"],
                )
                self.assertEqual(res.duplicates_skipped, exp["duplicates_skipped"])
                self.assertEqual(res.unreachable, exp["unreachable"])

    def test_all_cycles_match_hand_analysis(self):
        got = [list(c) for c in find_all_cycles(self.graph)]
        self.assertEqual(got, self.spec["all_cycles_expected"])

    def test_all_cycles_match_oracle(self):
        got = sorted(map(tuple, find_all_cycles(self.graph)))
        want = sorted(map(tuple, oracle_cycles(self.spec["graph"]["edges"])))
        self.assertEqual(got, want)

    def test_traversal_cycles_subset_of_all_cycles(self):
        res = recursive_query(self.graph, 0)
        all_keys = {Cycle.canonical_key(c) for c in find_all_cycles(self.graph)}
        for c in res.cycles:
            self.assertIn(c.key, all_keys)


class OraclePropertyTest(unittest.TestCase):
    """随机小图：Johnson 全量环枚举 vs 暴力 oracle。"""

    def test_random_graphs(self):
        rng = random.Random(20261005)
        for trial in range(300):
            n = rng.randint(1, 7)
            m = rng.randint(0, 14)
            edges = [
                (rng.randrange(n), rng.randrange(n))  # 允许自环/重复边
                for _ in range(m)
            ]
            got = sorted(map(tuple, find_all_cycles(Graph(edges))))
            want = sorted(map(tuple, oracle_cycles(edges)))
            self.assertEqual(got, want, msg="edges=%r" % (edges,))

    def test_deterministic_across_runs(self):
        edges = [(3, 1), (1, 2), (2, 3), (2, 0), (0, 0), (1, 1), (3, 1)]
        first = find_all_cycles(Graph(edges))
        for _ in range(5):
            self.assertEqual(find_all_cycles(Graph(edges)), first)


class EdgeCaseTest(unittest.TestCase):
    def test_empty_graph(self):
        res = recursive_query(Graph([]), 0)
        self.assertEqual(res.rows, [0])
        self.assertTrue(res.unreachable)
        self.assertEqual(res.shape, "ok")
        self.assertEqual(find_all_cycles(Graph([])), [])

    def test_duplicate_edges_ignored_and_counted(self):
        g = Graph([(0, 1), (0, 1), (0, 1), (1, 2)])
        self.assertEqual(g.duplicates_skipped, 2)
        res = recursive_query(g, 0)
        self.assertEqual(res.rows, [0, 1, 2])
        self.assertEqual(res.duplicates_skipped, 2)

    def test_self_loop_only(self):
        g = Graph([(0, 0)])
        res = recursive_query(g, 0)
        self.assertEqual([list(c.nodes) for c in res.cycles], [[0]])
        self.assertEqual(res.shape, "cycle")
        self.assertEqual(find_all_cycles(g), [(0,)])

    def test_no_reachable_path_and_unreachable_cycle_not_reported(self):
        g = Graph([(0, 1), (5, 6), (6, 5)])
        res = recursive_query(g, 0)
        self.assertEqual(res.rows, [0, 1])
        self.assertEqual(res.cycles, [])
        self.assertEqual([list(c) for c in find_all_cycles(g)], [[5, 6]])

    def test_isolated_root(self):
        g = Graph([(1, 2)])
        res = recursive_query(g, 99)
        self.assertEqual(res.rows, [99])
        self.assertTrue(res.unreachable)

    def test_multi_roots(self):
        g = Graph([(0, 1), (2, 3), (3, 2)])
        res = recursive_query(g, [0, 2])
        self.assertEqual(res.rows[:2], [0, 1])
        self.assertEqual([list(c.nodes) for c in res.cycles], [[2, 3]])

    def test_intersecting_cycles(self):
        # 两个环共享一条边：0->1->2->0 与 0->1->3->2->0
        edges = [(0, 1), (1, 2), (2, 0), (1, 3), (3, 2)]
        got = sorted(map(tuple, find_all_cycles(Graph(edges))))
        self.assertEqual(got, [(0, 1, 2), (0, 1, 3, 2)])

    def test_deep_acyclic_chain_no_recursion_error(self):
        n = 100_000
        g = Graph([(i, i + 1) for i in range(n)])
        res = recursive_query(g, 0)  # 不限深：0..n 全部访问
        self.assertEqual(len(res.rows), n + 1)
        self.assertEqual(res.cycles, [])
        self.assertEqual(res.truncated, [])
        res2 = recursive_query(g, 0, max_depth=10_000)
        self.assertEqual(res2.shape, "depth_limit")
        self.assertEqual(len(res2.truncated), 1)
        self.assertEqual(res2.truncated[0].at_depth, 10_001)
        # 节点 0..10000 + 1 个裁剪标记
        self.assertEqual(len(res2.rows), 10_002)

    def test_deep_chain_ending_in_cycle(self):
        # 1 万深链 + 末端三环，验证深链与环同时处理
        depth = 10_000
        edges = [(i, i + 1) for i in range(depth)]
        edges += [(depth, depth + 1), (depth + 1, depth + 2), (depth + 2, depth)]
        g = Graph(edges)
        res = recursive_query(g, 0)
        self.assertEqual([list(c.nodes) for c in res.cycles],
                         [[depth, depth + 1, depth + 2]])
        self.assertEqual(len(res.rows), depth + 4)  # 链上节点 + 3 环节点 + 1 环标记

    def test_max_depth_zero(self):
        g = Graph([(0, 1), (1, 2)])
        res = recursive_query(g, 0, max_depth=0)
        self.assertEqual(res.rows, [0, Truncation(1, 1)])
        self.assertEqual(res.shape, "depth_limit")

    def test_result_shapes_are_distinct_types(self):
        g = Graph([(0, 1), (1, 0), (1, 2), (2, 3)])
        res = recursive_query(g, 0, max_depth=2)
        kinds = {type(r) for r in res.rows}
        self.assertIn(Cycle, kinds)
        self.assertIn(Truncation, kinds)
        self.assertIn(int, kinds)
        self.assertEqual(res.shape, "cycle+depth_limit")


if __name__ == "__main__":
    unittest.main(verbosity=2)

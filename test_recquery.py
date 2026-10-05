"""recquery 自测：对拍手工分析数据 + 边界用例。运行：python3 -m unittest -v"""

import unittest

from recquery import build_graph, find_cycles, query
from cases import QUERY_CASES


def cycle_nodes(result):
    return [r["nodes"] for r in result["cycles"]]


def cycle_sequences(result):
    return [seq for r in result["cycles"] for seq in r["cycles"]]


class TestHandCheckedCases(unittest.TestCase):
    """对拍：每个用例的实际结果必须与 cases.py 中手工推导一致。"""

    def check_case(self, case):
        graph, dups = build_graph(case["edges"])
        result = query(graph, case["start"], case["max_depth"])
        self.assertEqual(result["status"], case["expected_status"], case["name"])
        self.assertEqual(result["visited"], case["expected_visited"], case["name"])
        self.assertEqual(result["truncated"], case["expected_truncated"], case["name"])
        self.assertEqual(cycle_nodes(result), case["expected_cycle_nodes"], case["name"])
        self.assertEqual(
            cycle_sequences(result), case["expected_cycle_sequences"], case["name"])
        if "expected_duplicates" in case:
            self.assertEqual(dups, case["expected_duplicates"], case["name"])

    def test_all_cases(self):
        for case in QUERY_CASES:
            with self.subTest(case=case["name"]):
                self.check_case(case)


class TestEdgeCases(unittest.TestCase):
    def test_start_not_in_graph(self):
        graph, _ = build_graph([(1, 2)])
        result = query(graph, "ghost", 5)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["visited"], {"ghost": 0})
        self.assertEqual(result["truncated"], [])
        self.assertEqual(result["cycles"], [])

    def test_max_depth_zero(self):
        graph, _ = build_graph([(1, 2), (2, 2)])
        result = query(graph, 1, 0)
        self.assertEqual(result["status"], "depth_limited")
        self.assertEqual(result["visited"], {1: 0})
        self.assertEqual(result["truncated"], [1])
        self.assertEqual(result["cycles"], [])  # 深度 0 看不到环

    def test_self_loop_only_node(self):
        graph, _ = build_graph([("s", "s")])
        result = query(graph, "s", 100)
        self.assertEqual(result["status"], "cycle_detected")
        self.assertEqual(cycle_sequences(result), [["s", "s"]])

    def test_deep_chain_no_recursion_error(self):
        # 深度 1 万的链：迭代实现不得抛 RecursionError，且全部可达。
        n = 10_000
        graph, _ = build_graph([(i, i + 1) for i in range(n)])
        result = query(graph, 0, n)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["visited"]), n + 1)
        self.assertEqual(result["visited"][n], n)

    def test_deep_chain_truncated_midway(self):
        n = 10_000
        graph, _ = build_graph([(i, i + 1) for i in range(n)])
        result = query(graph, 0, 5_000)
        self.assertEqual(result["status"], "depth_limited")
        self.assertEqual(result["truncated"], [5_000])
        self.assertEqual(len(result["visited"]), 5_001)

    def test_dense_back_edges_no_hang(self):
        # 全互连 200 节点：大量环，遍历与检测必须终止。
        n = 200
        graph, _ = build_graph(
            [(i, j) for i in range(n) for j in range(n) if i != j])
        result = query(graph, 0, 10, cycle_cap=50)
        self.assertEqual(result["status"], "cycle_detected")
        self.assertEqual(len(result["visited"]), n)
        self.assertTrue(result["cycles"][0]["cycles_truncated"])  # 枚举被 cap 截断
        self.assertEqual(len(result["cycles"][0]["nodes"]), n)    # 但环节点全集完整

    def test_find_cycles_whole_graph(self):
        # 不指定起点时，检测全图（含不可达环）。
        graph, _ = build_graph([("p", "q"), ("q", "p"), ("s", "t")])
        reports = find_cycles(graph)
        self.assertEqual([r["nodes"] for r in reports], [["p", "q"]])
        self.assertEqual(reports[0]["cycles"], [["p", "q", "p"]])

    def test_duplicate_and_self_loop_edges(self):
        graph, dups = build_graph([(1, 1), (1, 1), (1, 2), (1, 2)])
        self.assertEqual(dups, 2)
        self.assertEqual(graph[1], [1, 2])


if __name__ == "__main__":
    unittest.main()

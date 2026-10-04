"""slide_order 库自测：python3 -m unittest -v"""

import time
import unittest

from slide_order import (
    CYCLE, DANGLING, DUPLICATE, SELF_REF, UNREACHABLE, analyze,
)


def kinds(result):
    return [a.kind for a in result.anomalies]


class TestSlideOrder(unittest.TestCase):
    def test_single_slide(self):
        result = analyze({"slides": [1], "relations": {"1": []}})
        self.assertEqual(result.order, [1])
        self.assertEqual(result.anomalies, [])
        self.assertTrue(all(c.status == "match" for c in result.comparison))

    def test_consistent_with_numbering(self):
        result = analyze({
            "slides": [1, 2, 3, 4],
            "relations": {"1": [2], "2": [3], "3": [4], "4": []},
        })
        self.assertEqual(result.order, [1, 2, 3, 4])
        self.assertEqual(result.anomalies, [])
        self.assertTrue(all(c.status == "match" for c in result.comparison))

    def test_relations_override_numbering(self):
        # 编号顺序 1,2,3,4，但关系图要求 1->3->2->4
        result = analyze({
            "slides": [1, 2, 3, 4],
            "relations": {"1": [3], "3": [2], "2": [4], "4": []},
        })
        self.assertEqual(result.order, [1, 3, 2, 4])
        mismatched = {c.slide for c in result.comparison if c.status == "mismatch"}
        self.assertEqual(mismatched, {2, 3})
        self.assertEqual(result.anomalies, [])

    def test_cycle_reports_all_nodes_and_terminates(self):
        result = analyze({
            "slides": [1, 2, 3, 4],
            "relations": {"1": [2], "2": [3], "3": [4], "4": [2]},
        })
        self.assertEqual(result.order, [1, 2, 3, 4])  # 入环处截断，不无限循环
        self.assertEqual(result.cycles, [[2, 3, 4]])  # 环上全部节点
        self.assertIn(CYCLE, kinds(result))

    def test_isolated_cycle_among_unreachable(self):
        result = analyze({
            "slides": [1, 2, 5, 6, 7],
            "relations": {"1": [2], "2": [], "5": [6], "6": [7], "7": [5]},
        })
        self.assertEqual(result.order, [1, 2])
        self.assertEqual(sorted(result.unreachable), [5, 6, 7])
        self.assertIn([5, 6, 7], result.cycles)
        self.assertIn(UNREACHABLE, kinds(result))
        self.assertIn(CYCLE, kinds(result))  # 孤立环也必须产生异常条目

    def test_dangling_reference(self):
        result = analyze({
            "slides": [1, 2],
            "relations": {"1": [2], "2": [99]},
        })
        self.assertEqual(result.order, [1, 2])
        dangling = [a for a in result.anomalies if a.kind == DANGLING]
        self.assertEqual(len(dangling), 1)
        self.assertEqual(dangling[0].nodes, [2, 99])
        self.assertTrue(dangling[0].suggestion)

    def test_duplicate_references(self):
        # 后继列表内重复 + 多个前驱指向同一页
        result = analyze({
            "slides": [1, 2, 3, 4],
            "relations": {"1": [2, 2], "2": [4], "3": [4], "4": []},
        })
        dup = [a for a in result.anomalies if a.kind == DUPLICATE]
        self.assertEqual(len(dup), 2)
        self.assertEqual(result.order, [1, 2, 4])
        self.assertEqual(result.unreachable, [3])

    def test_self_reference(self):
        result = analyze({
            "slides": [1, 2],
            "relations": {"1": [1], "2": []},
        })
        self.assertEqual(result.order, [1])
        self.assertIn(SELF_REF, kinds(result))

    def test_missing_start(self):
        result = analyze({"start": 42, "slides": [1], "relations": {"1": []}})
        self.assertEqual(result.order, [])
        self.assertIn(DANGLING, kinds(result))

    def test_five_hundred_slides_perf_and_depth(self):
        n = 500
        # 编号打乱：文件号与放映顺序故意不一致
        order = list(range(1, n + 1))
        shuffled = order[::2] + order[1::2]
        relations = {str(shuffled[i]): [shuffled[i + 1]] for i in range(n - 1)}
        relations[str(shuffled[-1])] = []
        spec = {"start": shuffled[0], "slides": order, "relations": relations}

        begin = time.perf_counter()
        result = analyze(spec)
        wall_ms = (time.perf_counter() - begin) * 1000.0

        self.assertEqual(result.order, shuffled)
        self.assertEqual(result.max_chain_depth, n)
        self.assertEqual(result.call_stack_depth_bound, 0)  # 纯迭代，无递归
        self.assertLess(wall_ms, 1000, f"500 页解析应 < 1s，实际 {wall_ms:.2f} ms")
        mismatched = [c for c in result.comparison if c.status == "mismatch"]
        self.assertTrue(mismatched)  # 编号与顺序打架的页被逐页列出
        print(f"\n500 页解析耗时：{result.elapsed_ms:.3f} ms，"
              f"调用栈深度上界：{result.call_stack_depth_bound}，"
              f"最长链：{result.max_chain_depth}")


if __name__ == "__main__":
    unittest.main()

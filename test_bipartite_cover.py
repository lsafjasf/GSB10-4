"""bipartite_cover 的自测（仅标准库 unittest）。

运行: python3 test_bipartite_cover.py -v
"""

import itertools
import random
import unittest

from bipartite_cover import BipartiteGraph


def L(i):
    return ("L", i)


def R(j):
    return ("R", j)


def brute_force_min_cover(n_left, n_right, edges):
    """暴力枚举所有子集求最小点覆盖规模，仅用于小规模对照。"""
    best = None
    nodes_l = list(range(n_left))
    nodes_r = list(range(n_right))
    for k in range(n_left + n_right + 1):
        found = False
        for cl_size in range(max(0, k - n_right), min(k, n_left) + 1):
            cr_size = k - cl_size
            for cl in itertools.combinations(nodes_l, cl_size):
                sl = set(cl)
                for cr in itertools.combinations(nodes_r, cr_size):
                    sr = set(cr)
                    if all(u in sl or v in sr for u, v in edges):
                        return k
        if found:
            return best
    return n_left + n_right


class TestBasicCases(unittest.TestCase):
    def solve(self, n_left, n_right, edges):
        g = BipartiteGraph([L(i) for i in range(n_left)],
                           [R(j) for j in range(n_right)])
        g.add_edges([(L(u), R(v)) for u, v in edges])
        return g, g.min_vertex_cover()

    def test_single_edge(self):
        """覆盖单边：一条边只需一个端点。"""
        g, res = self.solve(1, 1, [(0, 0)])
        self.assertTrue(res.is_valid_cover())
        self.assertEqual(res.cover_size, 1)
        self.assertEqual(res.matching_size, 1)
        self.assertTrue(res.is_minimum())

    def test_disconnected(self):
        """不连通图：两个互不相连的部分，覆盖 = 各部分覆盖之和。"""
        # 组件1: L0-R0 单边（答案1）；组件2: L1 连 R1,R2 的星（答案1，选 L1）
        g, res = self.solve(2, 3, [(0, 0), (1, 1), (1, 2)])
        self.assertTrue(res.is_valid_cover())
        self.assertEqual(res.cover_size, 2)
        self.assertEqual(res.cover_size, res.matching_size)

    def test_empty_side(self):
        """一侧为空：没有边，空覆盖。"""
        g, res = self.solve(5, 0, [])
        self.assertTrue(res.is_valid_cover())
        self.assertEqual(res.cover_size, 0)
        self.assertEqual(res.matching_size, 0)
        g2, res2 = self.solve(0, 0, [])
        self.assertEqual(res2.cover_size, 0)

    def test_no_edges_isolated_vertices(self):
        """有点无边：空覆盖。"""
        g, res = self.solve(3, 4, [])
        self.assertTrue(res.is_valid_cover())
        self.assertEqual(res.cover_size, 0)

    def test_multi_edges(self):
        """多重边：被识别报告并去重，不影响覆盖正确性。"""
        g, res = self.solve(2, 2, [(0, 0), (0, 0), (0, 0), (1, 1), (0, 1)])
        self.assertEqual(len(g.multi_edges), 2)          # (0,0) 重复了 2 次
        self.assertEqual(len(g.edges), 3)                # 去重后 3 条唯一边
        self.assertTrue(res.is_valid_cover())
        self.assertEqual(res.cover_size, res.matching_size)
        self.assertEqual(res.cover_size, 2)

    def test_self_loop_and_same_side_reported(self):
        """自环与同侧边：必须识别并报告，不得静默忽略。"""
        g, res = self.solve(2, 2, [(0, 0), (1, 1)])     # 正常边
        g.add_edge(L(0), L(1))                          # 同侧边（都在左部）
        g.add_edge(L(0), R(0))                          # 多重边
        g.add_edge(L(0), L(0))                          # 自环
        kinds = [k for k, _, _ in g.anomalies]
        self.assertIn("same_side", kinds)
        self.assertIn("self_loop", kinds)
        self.assertEqual(len(g.multi_edges), 1)
        self.assertTrue(res.is_valid_cover())
        # 报告里必须出现这些异常
        text = res.report()
        self.assertIn("same_side", text)
        self.assertIn("self_loop", text)

    def test_path_and_cycle(self):
        """已知答案的结构：路径 P4（左右各2，边成链）覆盖=2；4-环 覆盖=2。"""
        # 4-环: L0-R0, L0-R1, L1-R0, L1-R1 -> K2,2，最小覆盖 = 2
        g, res = self.solve(2, 2, [(0, 0), (0, 1), (1, 0), (1, 1)])
        self.assertTrue(res.is_valid_cover())
        self.assertEqual(res.cover_size, 2)
        # 长链 L0-R0-L1-R1-L2-R2（6点路径）最小覆盖 = 3
        g2, res2 = self.solve(3, 3, [(0, 0), (1, 0), (1, 1), (2, 1), (2, 2)])
        self.assertTrue(res2.is_valid_cover())
        self.assertEqual(res2.cover_size, 3)

    def test_complete_bipartite(self):
        """K_{3,5}：最小覆盖 = min(3,5) = 3。"""
        edges = [(i, j) for i in range(3) for j in range(5)]
        g, res = self.solve(3, 5, edges)
        self.assertTrue(res.is_valid_cover())
        self.assertEqual(res.cover_size, 3)
        self.assertEqual(res.matching_size, 3)


class TestAgainstBruteForce(unittest.TestCase):
    """随机小图与暴力枚举对照：规模一致 + 逐边核验 + 下界取等。"""

    def test_random_small(self):
        rng = random.Random(20261005)
        for trial in range(300):
            n_left = rng.randint(0, 4)
            n_right = rng.randint(0, 4)
            all_edges = [(i, j) for i in range(n_left) for j in range(n_right)]
            edges = [e for e in all_edges if rng.random() < 0.5]
            g = BipartiteGraph([L(i) for i in range(n_left)],
                               [R(j) for j in range(n_right)])
            g.add_edges([(L(u), R(v)) for u, v in edges])
            res = g.min_vertex_cover()
            # 1) 逐边核验
            self.assertTrue(res.is_valid_cover(),
                            f"trial {trial}: 存在未覆盖边 {res.uncovered_edges()}")
            # 2) 与匹配下界一致
            self.assertEqual(res.cover_size, res.matching_size,
                             f"trial {trial}: |C| != |M|")
            # 3) 与暴力枚举的最小值一致
            bf = brute_force_min_cover(n_left, n_right, edges)
            self.assertEqual(res.cover_size, bf,
                             f"trial {trial}: |C|={res.cover_size} != 暴力解={bf}")


class TestKönigInvariant(unittest.TestCase):
    """较大随机图上验证 König 不变式 |C| == |M| 且覆盖有效。"""

    def test_random_medium(self):
        rng = random.Random(7)
        for trial in range(20):
            n_left = rng.randint(10, 60)
            n_right = rng.randint(10, 60)
            m = rng.randint(0, 400)
            edges = [(rng.randrange(n_left), rng.randrange(n_right))
                     for _ in range(m)]
            g = BipartiteGraph([L(i) for i in range(n_left)],
                               [R(j) for j in range(n_right)])
            g.add_edges([(L(u), R(v)) for u, v in edges])
            res = g.min_vertex_cover()
            self.assertTrue(res.is_minimum(), f"trial {trial} 失败")


if __name__ == "__main__":
    unittest.main(verbosity=2)

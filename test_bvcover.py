"""bvcover 自测：边界情形 + 性质核验 + 随机小图对暴力枚举。运行：python3 -m unittest -v"""

import itertools
import random
import unittest

from bvcover import LEFT, RIGHT, min_vertex_cover


def brute_force_min_cover(n, edges):
    """暴力枚举所有顶点子集求最小点覆盖（仅用于小规模独立对照）。"""
    edges = list(set(edges))
    for size in range(n + 1):
        for subset in itertools.combinations(range(n), size):
            s = set(subset)
            if all(a in s or b in s for a, b in edges):
                return size
    return n


class TestMinVertexCover(unittest.TestCase):

    def test_single_edge(self):
        res = min_vertex_cover(2, [LEFT, RIGHT], [(0, 1)])
        self.assertEqual(res.n_edges_valid, 1)
        self.assertEqual(res.matching_size, 1)
        self.assertEqual(res.cover_size, 1)
        self.assertTrue(res.verified)
        self.assertTrue(res.optimal)
        self.assertEqual(res.cover, [0])  # L\\Z 给出左端点

    def test_disconnected(self):
        # 两个互不相连的 K2 + 一个 4 点路径分量
        side = [LEFT, RIGHT, LEFT, RIGHT, LEFT, RIGHT, LEFT, RIGHT]
        edges = [(0, 1), (2, 3), (4, 5), (5, 6), (6, 7)]
        res = min_vertex_cover(8, side, edges)
        self.assertTrue(res.verified)
        self.assertTrue(res.optimal)
        self.assertEqual(res.matching_size, res.cover_size)
        self.assertEqual(res.cover_size, brute_force_min_cover(8, edges))

    def test_empty_side_no_left(self):
        # 左侧为空：无法形成二部边，覆盖必为空
        res = min_vertex_cover(3, [RIGHT, RIGHT, RIGHT], [])
        self.assertEqual((res.n_left, res.n_right), (0, 3))
        self.assertEqual(res.cover_size, 0)
        self.assertEqual(res.matching_size, 0)
        self.assertTrue(res.verified and res.optimal)

    def test_empty_side_no_right(self):
        res = min_vertex_cover(3, [LEFT, LEFT, LEFT], [])
        self.assertEqual((res.n_left, res.n_right), (3, 0))
        self.assertEqual(res.cover_size, 0)
        self.assertTrue(res.verified and res.optimal)

    def test_multiedges(self):
        # (0,1) 重复 3 次（2 条冗余），(2,3) 重复 2 次（1 条冗余）
        edges = [(0, 1), (1, 0), (0, 1), (2, 3), (3, 2)]
        side = [LEFT, LEFT, LEFT, RIGHT]
        # 重新编号：0,1,2 左；3 右 —— 调整边使其跨侧
        edges = [(0, 3), (3, 0), (0, 3), (2, 3), (3, 2)]
        res = min_vertex_cover(4, side, edges)
        self.assertEqual(res.n_edges_input, 5)
        self.assertEqual(res.n_edges_valid, 2)
        self.assertEqual(res.n_duplicate_extra, 3)
        self.assertEqual(len(res.duplicate_examples), 3)
        self.assertTrue(res.verified and res.optimal)
        self.assertEqual(res.cover_size, 1)  # 星型，取右点 3 即可
        self.assertEqual(res.cover, [3])

    def test_same_side_edges_and_self_loops(self):
        # 自环 (0,0)、左侧内边 (0,1)、右侧内边 (2,3) 必须报告
        side = [LEFT, LEFT, RIGHT, RIGHT]
        edges = [(0, 0), (0, 1), (2, 3), (0, 2)]
        res = min_vertex_cover(4, side, edges)
        self.assertEqual(res.n_same_side, 3)
        self.assertEqual(len(res.same_side_examples), 3)
        self.assertEqual(res.n_edges_valid, 1)
        self.assertTrue(res.verified and res.optimal)
        self.assertEqual(res.cover_size, 1)

    def test_empty_graph(self):
        res = min_vertex_cover(4, [LEFT, RIGHT, LEFT, RIGHT], [])
        self.assertEqual(res.cover_size, 0)
        self.assertTrue(res.verified and res.optimal)

    def test_known_figure_path4(self):
        # 路径 u0-v0-u1-v1：最小覆盖可取两个中点
        side = [LEFT, RIGHT, LEFT, RIGHT]
        edges = [(0, 1), (2, 1), (2, 3)]
        res = min_vertex_cover(4, side, edges)
        self.assertTrue(res.verified and res.optimal)
        self.assertEqual(res.cover_size, 2)
        self.assertEqual(res.cover_size, brute_force_min_cover(4, edges))

    def test_random_against_brute_force(self):
        rng = random.Random(20261005)
        for seed in range(300):
            rng.seed(seed)
            n = rng.randint(1, 10)
            side = [rng.choice([LEFT, RIGHT]) for _ in range(n)]
            m = rng.randint(0, n * (n + 1) // 2)
            edges = [(rng.randrange(n), rng.randrange(n)) for _ in range(m)]
            res = min_vertex_cover(n, side, edges)
            self.assertTrue(res.verified, seed)
            self.assertTrue(res.optimal, seed)
            # 暴力只看跨侧的去重边（按 左,右 规范化，与库的去重口径一致）
            canon = set()
            for a, b in edges:
                if side[a] != side[b]:
                    canon.add((a, b) if side[a] == LEFT else (b, a))
            valid = sorted(canon)
            self.assertEqual(res.cover_size, brute_force_min_cover(n, valid), seed)
            self.assertEqual(res.n_edges_valid, len(valid), seed)
            self.assertEqual(res.n_same_side,
                             sum(1 for a, b in edges if side[a] == side[b]), seed)

    def test_medium_random_properties(self):
        rng = random.Random(42)
        n_left, n_right = 200, 200
        edges = [(rng.randrange(n_left), n_left + rng.randrange(n_right))
                 for _ in range(2000)]
        side = [LEFT] * n_left + [RIGHT] * n_right
        res = min_vertex_cover(n_left + n_right, side, edges)
        self.assertTrue(res.verified)
        self.assertTrue(res.optimal)
        self.assertEqual(res.edges_checked, res.n_edges_valid)


if __name__ == "__main__":
    unittest.main(verbosity=2)

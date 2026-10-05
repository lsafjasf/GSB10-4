"""自测：覆盖全正权、含负权无负环、可达负环、不可达负环、零权环、自环，
并与参考实现（SPFA）逐点对拍。运行：python3 test_shortest_path.py -v
"""

import random
import unittest

from shortest_path import (
    INF,
    bellman_ford,
    cycle_weight,
    find_any_negative_cycle,
    has_negative_weight_edge,
    spfa_reference,
)


def gen_graph_no_neg_cycle(n, m, rng, max_w=10, return_h=False):
    """用势函数 reweighting 生成含负权边但保证无负环的图：
    w(u,v) = base(>=0) + h[u] - h[v]  =>  任意环的权和 = sum(base) >= 0。"""
    h = [rng.randint(-50, 50) for _ in range(n)]
    edges = []
    for _ in range(m):
        u = rng.randrange(n)
        v = rng.randrange(n)
        w = rng.randint(0, max_w) + h[u] - h[v]
        edges.append((u, v, w))
    return (edges, h) if return_h else edges


class TestBasicCases(unittest.TestCase):
    def test_all_positive(self):
        # 全正权：0->1(3), 0->2(1), 2->1(1), 1->3(2), 2->3(5)
        edges = [(0, 1, 3), (0, 2, 1), (2, 1, 1), (1, 3, 2), (2, 3, 5)]
        r = bellman_ford(4, edges, 0)
        self.assertFalse(r.has_neg_cycle)
        self.assertIsNone(r.cycle)
        self.assertEqual(r.dist, [0, 2, 1, 4])
        self.assertFalse(has_negative_weight_edge(edges))

    def test_negative_edges_no_cycle(self):
        # 含负权边、无负环：0->1(4), 0->2(5), 1->2(-3), 2->3(2), 1->3(4)
        edges = [(0, 1, 4), (0, 2, 5), (1, 2, -3), (2, 3, 2), (1, 3, 4)]
        r = bellman_ford(4, edges, 0)
        self.assertFalse(r.has_neg_cycle)
        self.assertEqual(r.dist, [0, 4, 1, 3])
        self.assertTrue(has_negative_weight_edge(edges))  # 负权边 ≠ 负环

    def test_reachable_negative_cycle(self):
        # 可达负环 1->2->3->1，权和 = 1+1-5 = -3 < 0
        edges = [(0, 1, 1), (1, 2, 1), (2, 3, 1), (3, 1, -5)]
        r = bellman_ford(4, edges, 0)
        self.assertTrue(r.has_neg_cycle)
        self.assertTrue(r.cycle_reachable)
        self.assertIsNotNone(r.cycle)
        self.assertEqual(set(r.cycle), {1, 2, 3})
        self.assertLess(cycle_weight(r.cycle, edges), 0)
        print(f"\n[可达负环] 环节点: {r.cycle}, 环权: {cycle_weight(r.cycle, edges)}")

    def test_unreachable_negative_cycle(self):
        # 源点 0 所在分量无环；分量 {3,4} 含负环 3->4->3，权和 = 2-5 = -3
        edges = [(0, 1, 2), (1, 2, 3), (3, 4, 2), (4, 3, -5)]
        r = bellman_ford(5, edges, 0)
        # 从源点看：负环不可达 => 最短路径有定义
        self.assertFalse(r.has_neg_cycle)
        self.assertEqual(r.dist, [0, 2, 5, INF, INF])
        # 全局检测：能找出不可达负环
        cyc = find_any_negative_cycle(5, edges)
        self.assertIsNotNone(cyc)
        self.assertEqual(set(cyc), {3, 4})
        self.assertLess(cycle_weight(cyc, edges), 0)
        print(f"\n[不可达负环] 环节点: {cyc}, 环权: {cycle_weight(cyc, edges)}, "
              f"源点出发 dist={r.dist}")

    def test_zero_weight_cycle_not_misjudged(self):
        # 零权环 1->2->3->1，权和 = 2-1-1 = 0，不得误判
        edges = [(0, 1, 1), (1, 2, 2), (2, 3, -1), (3, 1, -1)]
        r = bellman_ford(4, edges, 0)
        self.assertFalse(r.has_neg_cycle)
        self.assertIsNone(find_any_negative_cycle(4, edges))
        self.assertEqual(r.dist, [0, 1, 3, 2])

    def test_nonnegative_self_loop_not_misjudged(self):
        # 零权/正权自环不得误判为负环
        edges = [(0, 0, 0), (0, 1, 2), (1, 1, 5)]
        r = bellman_ford(2, edges, 0)
        self.assertFalse(r.has_neg_cycle)
        self.assertIsNone(find_any_negative_cycle(2, edges))
        self.assertEqual(r.dist, [0, 2])

    def test_negative_self_loop_is_negative_cycle(self):
        # 负权自环是长度为 1 的负环
        edges = [(0, 1, 1), (1, 1, -2)]
        r = bellman_ford(2, edges, 0)
        self.assertTrue(r.has_neg_cycle)
        self.assertEqual(r.cycle, [1])

    def test_disconnected_source(self):
        edges = [(1, 2, 7)]
        r = bellman_ford(3, edges, 0)
        self.assertFalse(r.has_neg_cycle)
        self.assertEqual(r.dist, [0, INF, INF])


class TestCrossCheck(unittest.TestCase):
    """与参考实现 SPFA 逐点对拍（无负环图，含负权边）。"""

    def test_random_cross_check(self):
        rng = random.Random(20261005)
        total_nodes = 0
        mismatches = 0
        samples = []
        for trial in range(200):
            n = rng.randint(2, 60)
            m = rng.randint(1, 200)
            edges = gen_graph_no_neg_cycle(n, m, rng)
            src = rng.randrange(n)
            r = bellman_ford(n, edges, src)
            self.assertFalse(r.has_neg_cycle)
            ref = spfa_reference(n, edges, src)
            self.assertEqual(len(r.dist), len(ref))
            for i in range(n):
                total_nodes += 1
                if r.dist[i] != ref[i]:
                    mismatches += 1
            if trial < 3:
                samples.append((trial, n, m, src, r.dist[:8], ref[:8]))
        self.assertEqual(mismatches, 0)
        print(f"\n[对拍] 200 个随机图（含负权边、无负环），逐点比较 "
              f"{total_nodes} 个距离值，不一致 {mismatches} 处")
        for t, n, m, s, d, f in samples:
            print(f"  图{t}: n={n} m={m} src={s}\n"
                  f"    bellman_ford 前8点: {d}\n"
                  f"    spfa_reference 前8点: {f}")




class TestEarlyCycleDetection(unittest.TestCase):
    """强制走"提前检测"路径（图够大，第 n 轮之前就必须判定出负环）。"""

    def test_large_graph_early_exit(self):
        rng = random.Random(7)
        n, m = 3000, 12000
        edges = gen_graph_no_neg_cycle(n, m, rng)
        # 叠加一个源点可达负环 0->1->2->0
        edges += [(0, 1, 1), (1, 2, 1), (2, 0, -5)]
        r = bellman_ford(n, edges, 0)
        self.assertTrue(r.has_neg_cycle)
        self.assertLess(r.rounds, n)          # 提前检出，未跑满 n 轮
        self.assertLess(cycle_weight(r.cycle, edges), 0)  # 检出的环确为负环
        print(f"\n[提前检测] n={n} 第 {r.rounds} 轮检出, 环 {r.cycle}, "
              f"环权 {cycle_weight(r.cycle, edges)}")

    def test_early_check_does_not_misjudge_zero_cycle(self):
        # 大图中放零权环：提前检测路径也不得误判
        rng = random.Random(8)
        n, m = 3000, 12000
        edges, h = gen_graph_no_neg_cycle(n, m, rng, return_h=True)
        # 边权取 h[u]-h[v]：0->1->2->0 环权严格为 0，
        # 且任意含这些边的环权仍 >= 0（势函数性质），含负权边也不产生负环
        edges += [(0, 1, h[0] - h[1]), (1, 2, h[1] - h[2]),
                  (2, 0, h[2] - h[0])]
        self.assertTrue(any(w < 0 for _, _, w in edges))
        r = bellman_ford(n, edges, 0)
        self.assertFalse(r.has_neg_cycle)
        self.assertIsNone(find_any_negative_cycle(n, edges))


if __name__ == "__main__":
    unittest.main(verbosity=2)

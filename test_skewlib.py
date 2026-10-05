"""skewlib 自测：热点识别边界、四类倾斜情形、对拍（拆分-合并 vs 不拆分）。

运行：python3 -m unittest -v test_skewlib
"""

import random
import unittest
from collections import Counter

import skewlib as sk


def build_rows(spec):
    """spec: [(key, count), ...] -> 行序列（热键的行集中出现，模拟真实顺序）。"""
    rows = []
    for key, count in spec:
        rows.extend([key] * count)
    return rows


def differential_check(spec, num_partitions, ratio=0.1, abs_min=1):
    """核心对拍：合并结果 == 基线结果，并返回 (plan, rows, baseline, merged)。"""
    rows = build_rows(spec)
    counts = Counter(rows)
    plan = sk.SkewPlan(counts, num_partitions, ratio=ratio, abs_min=abs_min)
    baseline = sk.run_baseline(rows)
    parts = sk.run_partitioned(rows, plan)
    merged = sk.merge_partitions(parts)
    assert merged == baseline, (
        f"对拍失败\n合并: {dict(merged)}\n基线: {dict(baseline)}"
    )
    # 分区负载之和等于总行数，且子分片大小之和等于热键原始行数
    assert sum(plan.loads) == len(rows)
    for key, sizes in plan.shard_distribution().items():
        assert sum(sizes) == counts[key]
    return plan, rows, baseline, merged, parts


class ThresholdTests(unittest.TestCase):
    def test_threshold_exact_arithmetic(self):
        # total=1000, ratio=0.1 -> 精确 100，不能因浮点变成 101
        self.assertEqual(sk.threshold_for(1000, 0.1, 1), 100)
        self.assertEqual(sk.threshold_for(999, 0.1, 1), 100)   # ceil(99.9)=100
        self.assertEqual(sk.threshold_for(100, 0.1, 1), 10)
        # abs_min 取大
        self.assertEqual(sk.threshold_for(100, 0.1, 50), 50)

    def test_boundary_equal_is_cold(self):
        """刚好等于阈值：不是热点；阈值+1：才是热点（确定性）。"""
        spec = [("eq100", 100), ("hot101", 101), ("cold99", 99)]
        # 注意 eq100/hot101 之外的冷键会摊薄占比，故用 abs_min 固定绝对量判定
        counts = Counter(build_rows(spec))
        # ratio 设极小，让阈值完全由 abs_min=100 控制，精确卡边界
        hot, thr = sk.identify_hot_keys(counts, ratio=1e-9, abs_min=100)
        self.assertEqual(thr, 100)
        self.assertEqual(set(hot), {"hot101"}, hot)
        self.assertNotIn("eq100", hot)
        self.assertNotIn("cold99", hot)

        plan, rows, baseline, merged, parts = differential_check(
            spec, num_partitions=4, ratio=1e-9, abs_min=100
        )
        self.assertEqual(plan.num_splits, {"hot101": 2})
        # eq100 被当冷键：整键进一个分区，不产生子分片
        self.assertIn(("eq100", None), plan.partition_of)
        self.assertNotIn(("eq100", 0), plan.partition_of)

    def test_boundary_ratio_path(self):
        """占比路径上的边界：total=1000，100 行整（=10%）冷，101 行热。"""
        spec = [("a", 101)] + [(f"c{i}", 1) for i in range(899)]
        plan, *_ = differential_check(
            spec, num_partitions=8, ratio=0.1, abs_min=1
        )
        self.assertEqual(plan.threshold, 100)
        self.assertEqual(list(plan.hot), ["a"])

        spec2 = [("a", 100)] + [(f"c{i}", 1) for i in range(900)]
        plan2, *_ = differential_check(
            spec2, num_partitions=8, ratio=0.1, abs_min=1
        )
        self.assertEqual(plan2.hot, {})  # 恰好 10% 不算热点


class ScenarioTests(unittest.TestCase):
    def test_uniform(self):
        """完全均匀：100 个键各 100 行，无热点。"""
        spec = [(f"k{i:03d}", 100) for i in range(100)]
        plan, rows, baseline, merged, parts = differential_check(
            spec, num_partitions=8
        )
        self.assertEqual(plan.hot, {})
        self.assertEqual(plan.num_splits, {})
        # 无热点时每个键只进一个分区（整键哈希）
        for p in parts:
            for (_k, salt), _v in p.items():
                self.assertIsNone(salt)

    def test_single_hot(self):
        """单热点：1 个键占 50%，其余 500 个键均匀。"""
        spec = [("HOT", 5000)] + [(f"c{i}", 10) for i in range(500)]
        plan, rows, baseline, merged, parts = differential_check(
            spec, num_partitions=8
        )
        self.assertEqual(list(plan.hot), ["HOT"])
        self.assertEqual(sum(plan.shard_distribution()["HOT"]), 5000)
        # 拆分后最重分区负载应明显低于"不拆分时热键整键落一个分区"
        worst = max(plan.loads)
        self.assertLess(worst, 5000)
        # 热键每个子分片在不同（或可重复）分区，且覆盖多个分区
        hot_parts = {
            plan.partition_of[("HOT", i)]
            for i in range(plan.num_splits["HOT"])
        }
        self.assertGreater(len(hot_parts), 1)

    def test_multi_hot(self):
        """多热点：3 个热键分别占 ~30%/20%/15%。"""
        spec = (
            [("H1", 3000), ("H2", 2000), ("H3", 1500)]
            + [(f"c{i}", 7) for i in range(500)]  # 3500
        )
        plan, rows, baseline, merged, parts = differential_check(
            spec, num_partitions=8
        )
        self.assertEqual(list(plan.hot), ["H1", "H2", "H3"])
        for h, c in [("H1", 3000), ("H2", 2000), ("H3", 1500)]:
            sizes = plan.shard_distribution()[h]
            self.assertEqual(sum(sizes), c)
            self.assertEqual(max(sizes) - min(sizes) <= 1, True)
        # LPT 后最重分区不超过任一热键的原始体量
        self.assertLessEqual(max(plan.loads), 3000)

    def test_all_same_key(self):
        """全部同键：唯一键必为热点，应被均匀摊到所有分区。"""
        spec = [("ONLY", 1000)]
        plan, rows, baseline, merged, parts = differential_check(
            spec, num_partitions=8
        )
        self.assertEqual(list(plan.hot), ["ONLY"])
        sizes = plan.shard_distribution()["ONLY"]
        # 1000 行 / 8 分区 -> 8 片，每片 125
        self.assertEqual(len(sizes), 8)
        self.assertTrue(all(s == 125 for s in sizes))
        self.assertTrue(all(load == 125 for load in plan.loads))
        self.assertEqual(merged, {"ONLY": 1000})

    def test_split_sizes_deterministic(self):
        """轮询拆分大小分布：余数分布在前几片，最大差 1。"""
        self.assertEqual(sk.split_sizes(10, 3), [4, 3, 3])
        self.assertEqual(sk.split_sizes(9, 3), [3, 3, 3])
        self.assertEqual(sk.split_sizes(1, 2), [1, 0])

    def test_routing_round_robin_matches_distribution(self):
        """逐行路由后各子分片的实际计数必须等于 split_sizes 的预测。"""
        spec = [("HOT", 37), ("a", 5), ("b", 4)]
        rows = build_rows(spec)
        plan = sk.SkewPlan(Counter(rows), 4, ratio=0.1, abs_min=10)
        parts = sk.run_partitioned(rows, plan)
        n = plan.num_splits["HOT"]
        actual = [0] * n
        for p in parts:
            for (k, salt), v in p.items():
                if k == "HOT":
                    actual[salt] += v
        self.assertEqual(actual, sk.split_sizes(37, n))


class FuzzTests(unittest.TestCase):
    def test_random_differential(self):
        """随机 fuzz：多种分区数/阈值/键分布下，合并恒等于基线。"""
        rng = random.Random(20261005)
        for trial in range(60):
            kind = rng.choice(["uniform", "hot", "multi", "same", "sparse"])
            if kind == "uniform":
                spec = [(f"k{i}", rng.randint(1, 20))
                        for i in range(rng.randint(1, 200))]
            elif kind == "hot":
                spec = [("H", rng.randint(200, 2000))]
                spec += [(f"c{i}", rng.randint(1, 5))
                         for i in range(rng.randint(1, 300))]
            elif kind == "multi":
                spec = [(f"H{j}", rng.randint(100, 800)) for j in range(3)]
                spec += [(f"c{i}", rng.randint(1, 9))
                         for i in range(rng.randint(1, 200))]
            elif kind == "same":
                spec = [("X", rng.randint(1, 5000))]
            else:
                spec = [(f"k{i}", 1) for i in range(rng.randint(1, 50))]
            spec = [s for s in spec if s[1] > 0]
            if not spec:
                continue
            p_count = rng.choice([1, 2, 4, 8, 16])
            ratio = rng.choice([0.05, 0.1, 0.25])
            differential_check(spec, p_count, ratio=ratio, abs_min=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

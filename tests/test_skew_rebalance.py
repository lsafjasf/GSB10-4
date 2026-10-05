# -*- coding: utf-8 -*-
"""skew_rebalance 自测：识别依据、拆分分布、合并对拍、边界、四种数据形态。"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from skew_rebalance import (
    SkewConfig,
    build_split_plan,
    count_keys,
    identify_hot_keys,
    merge_upstream,
    partition_loads,
    partition_records,
    plain_partition_and_aggregate,
    split_partition_and_aggregate,
    split_factor,
    sub_partition_sizes,
    target_load,
)


def make_records(key_counts, seed=42):
    """按 {key: count} 生成记录，value 为确定性伪随机整数。"""
    rng = random.Random(seed)
    records = []
    for key, cnt in key_counts.items():
        for _ in range(cnt):
            records.append({"key": key, "value": rng.randint(-1000, 1000)})
    rng.shuffle(records)
    return records


class TestHotKeyIdentification(unittest.TestCase):
    def setUp(self):
        self.cfg = SkewConfig(num_partitions=8, share_threshold=0.2, abs_threshold=100)

    def test_uniform_no_hot_key(self):
        # 完全均匀：100 个键各 100 条，占比 1%，无热点
        counts = {f"k{i}": 100 for i in range(100)}
        total = sum(counts.values())
        self.assertEqual(identify_hot_keys(counts, total, self.cfg), {})

    def test_single_hot_key(self):
        counts = {"hot": 9000, **{f"k{i}": 10 for i in range(100)}}
        total = sum(counts.values())
        hot = identify_hot_keys(counts, total, self.cfg)
        self.assertEqual(hot, {"hot": 9000})

    def test_multi_hot_keys(self):
        counts = {"h1": 4000, "h2": 3000, "h3": 2500,
                  **{f"k{i}": 10 for i in range(50)}}
        total = sum(counts.values())
        hot = identify_hot_keys(counts, total, self.cfg)
        self.assertEqual(set(hot), {"h1", "h2", "h3"})

    def test_all_same_key(self):
        counts = {"only": 5000}
        hot = identify_hot_keys(counts, 5000, self.cfg)
        self.assertEqual(hot, {"only": 5000})

    def test_abs_threshold_blocks_small_dataset(self):
        # 占比 100% 但绝对量 < abs_threshold，不算热点
        counts = {"tiny": 50}
        self.assertEqual(identify_hot_keys(counts, 50, self.cfg), {})


class TestBoundary(unittest.TestCase):
    """刚好等于阈值 -> 热点；差 1 -> 非热点。两个方向都确定。"""

    def test_equal_abs_threshold_is_hot(self):
        cfg = SkewConfig(num_partitions=4, share_threshold=0.5, abs_threshold=100)
        # edge: 绝对量==100, 占比==0.5, 双阈值都恰好压线 -> 热点
        counts = {"edge": 100, **{f"c{i}": 1 for i in range(100)}}
        hot = identify_hot_keys(counts, 200, cfg)
        self.assertEqual(hot, {"edge": 100})

    def test_one_below_abs_threshold_not_hot(self):
        cfg = SkewConfig(num_partitions=4, share_threshold=0.5, abs_threshold=100)
        counts = {"edge": 99, **{f"c{i}": 1 for i in range(99)}}
        self.assertEqual(identify_hot_keys(counts, 198, cfg), {})

    def test_equal_share_threshold_is_hot(self):
        cfg = SkewConfig(num_partitions=4, share_threshold=0.25, abs_threshold=10)
        counts = {"edge": 250, **{f"k{i}": 150 for i in range(5)}}  # 250/1000 == 0.25
        hot = identify_hot_keys(counts, 1000, cfg)
        self.assertEqual(hot, {"edge": 250})

    def test_one_below_share_threshold_not_hot(self):
        cfg = SkewConfig(num_partitions=4, share_threshold=0.25, abs_threshold=10)
        counts = {"edge": 249, **{f"k{i}": 150 for i in range(5)}}  # 249/999 < 0.25
        self.assertEqual(identify_hot_keys(counts, 999, cfg), {})


class TestSplitPlan(unittest.TestCase):
    def test_split_factor_math(self):
        self.assertEqual(split_factor(9000, 1000), 9)
        self.assertEqual(split_factor(9001, 1000), 10)
        self.assertEqual(split_factor(1000, 1000), 1)

    def test_sub_partition_sizes_differ_by_at_most_one(self):
        sizes = sub_partition_sizes(9000, 8)
        self.assertEqual(sum(sizes), 9000)
        self.assertLessEqual(max(sizes) - min(sizes), 1)
        self.assertEqual(sizes, [1125] * 8)
        sizes = sub_partition_sizes(9001, 8)
        self.assertEqual(sum(sizes), 9001)
        self.assertLessEqual(max(sizes) - min(sizes), 1)

    def test_all_same_key_splits_to_num_partitions(self):
        # 全部同键：count == total，splits == num_partitions，子分区完全均匀
        cfg = SkewConfig(num_partitions=8, share_threshold=0.2, abs_threshold=10)
        counts = {"only": 8000}
        plan = build_split_plan(counts, 8000, cfg)
        self.assertEqual(plan, {"only": 8})
        self.assertEqual(sub_partition_sizes(8000, 8), [1000] * 8)


class TestMergeEquality(unittest.TestCase):
    """对拍：拆分路径合并结果必须与未拆分基线完全一致。"""

    def run_pair(self, key_counts, cfg, seed=42):
        records = make_records(key_counts, seed)
        counts = count_keys(records)
        plan = build_split_plan(counts, len(records), cfg)
        base_result, base_loads = plain_partition_and_aggregate(records, cfg)
        split_result, split_loads = split_partition_and_aggregate(records, cfg, plan)
        self.assertEqual(split_result, base_result)
        # 对拍数据也校验记录级多重集合一致（合并后每个键的记录集合不变）
        buckets = partition_records(records, cfg, plan)
        merged = {}
        for b in buckets:
            for r in b:
                merged.setdefault(r["key"], []).append(r["value"])
        for key, vals in merged.items():
            self.assertEqual(
                sorted(vals),
                sorted(r["value"] for r in records if r["key"] == key),
            )
        return plan, base_loads, split_loads

    def test_uniform(self):
        cfg = SkewConfig(num_partitions=8, share_threshold=0.2, abs_threshold=100)
        plan, _, _ = self.run_pair({f"k{i}": 50 for i in range(100)}, cfg)
        self.assertEqual(plan, {})  # 均匀数据不触发拆分

    def test_single_hot(self):
        cfg = SkewConfig(num_partitions=8, share_threshold=0.2, abs_threshold=100)
        key_counts = {"hot": 9000, **{f"k{i}": 10 for i in range(100)}}
        plan, base_loads, split_loads = self.run_pair(key_counts, cfg)
        self.assertIn("hot", plan)
        self.assertLess(max(split_loads), max(base_loads))  # 拆分确实削峰

    def test_multi_hot(self):
        cfg = SkewConfig(num_partitions=8, share_threshold=0.15, abs_threshold=100)
        key_counts = {"h1": 3000, "h2": 2500, "h3": 1500,
                      **{f"k{i}": 20 for i in range(50)}}
        plan, _, split_loads = self.run_pair(key_counts, cfg)
        self.assertEqual(set(plan), {"h1", "h2", "h3"})
        # 每个热点键自身的子分区大小相差至多 1
        for key, splits in plan.items():
            sizes = sub_partition_sizes(key_counts[key], splits)
            self.assertLessEqual(max(sizes) - min(sizes), 1)
        # 整体峰值显著低于未拆分基线
        _, base_loads = plain_partition_and_aggregate(make_records(key_counts), cfg)
        self.assertLess(max(split_loads), max(base_loads))

    def test_all_same_key(self):
        cfg = SkewConfig(num_partitions=8, share_threshold=0.2, abs_threshold=10)
        plan, base_loads, split_loads = self.run_pair({"only": 8000}, cfg)
        self.assertEqual(plan, {"only": 8})
        self.assertEqual(max(split_loads), 1000)
        self.assertEqual(min(split_loads), 1000)
        self.assertEqual(max(base_loads), 8000)  # 基线全部压在一个分区

    def test_boundary_key_merge_equal(self):
        # 边界键（恰好等于阈值）走拆分路径，合并结果仍一致
        cfg = SkewConfig(num_partitions=4, share_threshold=0.5, abs_threshold=100)
        self.run_pair({"edge": 100, "other": 100}, cfg)


class TestDeterminism(unittest.TestCase):
    def test_same_input_same_plan_and_route(self):
        cfg = SkewConfig(num_partitions=8, share_threshold=0.2, abs_threshold=100)
        records = make_records({"hot": 5000, "a": 100, "b": 100}, seed=7)
        counts = count_keys(records)
        plan1 = build_split_plan(counts, len(records), cfg)
        plan2 = build_split_plan(counts, len(records), cfg)
        self.assertEqual(plan1, plan2)
        loads1 = partition_loads(partition_records(records, cfg, plan1))
        loads2 = partition_loads(partition_records(records, cfg, plan2))
        self.assertEqual(loads1, loads2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

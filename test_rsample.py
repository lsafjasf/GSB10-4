#!/usr/bin/env python3
"""rsample 自测（标准库 unittest）。

运行: python3 -m unittest -v test_rsample
"""

import math
import random
import time
import unittest
from collections import Counter

from rsample import HashSampler

# 黄金向量：BLAKE2b-64(key, key=seed)，若哈希定义被改动应立即失败
GOLDEN = [
    (42, "", 8772462137215844456),
    (42, "a", 18015743052029225160),
    (42, "hello", 2713640981205549633),
    (42, "row-0", 6683597604395648782),
    (42, "row-1", 16475028453484122688),
    (42, "键-中文", 1670416085508631940),
    ("demo-seed", "hello", 11466731208505718138),
]


def chernoff_abs_bound(n, p, delta):
    """Chernoff 上界: P(|X-np| >= delta*np) <= 2*exp(-np*delta^2/3),
    返回在该概率保证下允许的绝对偏差（向上取整）。"""
    if n * p == 0:
        return 0.0
    # 求 delta 使 2*exp(-np*delta^2/3) <= alpha
    alpha = 1e-6
    delta = math.sqrt(-3.0 * math.log(alpha / 2.0) / (n * p))
    return n * p * delta


class TestDeterminism(unittest.TestCase):
    def test_golden_vectors(self):
        for seed, key, expected in GOLDEN:
            self.assertEqual(HashSampler(seed, 0.5).hash64(key), expected)

    def test_same_seed_same_result(self):
        a = HashSampler(20261007, 0.37)
        b = HashSampler(20261007, 0.37)
        keys = [f"k-{i}" for i in range(5000)]
        self.assertEqual([a.keep(k) for k in keys], [b.keep(k) for k in keys])

    def test_different_seed_differs(self):
        # 不同种子应产生明显不同的样本集合（不是硬保证，检查一致性比例合理）
        keys = [f"k-{i}" for i in range(10000)]
        a = set(k for k in keys if HashSampler(1, 0.5).keep(k))
        b = set(k for k in keys if HashSampler(2, 0.5).keep(k))
        agree = len(a & b) / 10000
        # p=0.5 时交集期望 0.25，留宽界
        self.assertLess(abs(agree - 0.25), 0.05)


class TestSamplingRate(unittest.TestCase):
    def test_rate_approx(self):
        n, p = 200000, 0.1
        s = HashSampler(7, p)
        cnt = sum(s.keep(f"row-{i}") for i in range(n))
        bound = chernoff_abs_bound(n, p, None)
        self.assertLess(abs(cnt - n * p), bound)

    def test_rate_quantiles_several_p(self):
        # 多个采样率 + 多个种子，全部落在 Chernoff 1e-6 界内
        for p in (0.001, 0.01, 0.1, 0.5):
            n = 300000
            bound = chernoff_abs_bound(n, p, None)
            for seed in (1, 2, 3):
                s = HashSampler(seed, p)
                cnt = sum(s.keep(f"x-{seed}-{i}") for i in range(n))
                self.assertLess(abs(cnt - n * p), bound,
                                f"p={p} seed={seed} cnt={cnt} bound={bound}")


class TestEdgeCases(unittest.TestCase):
    def test_rate_zero(self):
        s = HashSambler = HashSampler(123, 0.0)
        self.assertTrue(all(not s.keep(f"k{i}") for i in range(10000)))
        self.assertEqual(s.threshold, 0)

    def test_rate_one(self):
        s = HashSampler(123, 1.0)
        self.assertTrue(all(s.keep(f"k{i}") for i in range(10000)))
        self.assertEqual(s.threshold, 1 << 64)

    def test_invalid_rate(self):
        with self.assertRaises(ValueError):
            HashSampler(1, -0.1)
        with self.assertRaises(ValueError):
            HashSampler(1, 1.1)

    def test_tiny_population(self):
        # 样本极少：结果仍确定，且每个键决策可枚举
        keys = ["only-one"]
        s = HashSampler(99, 0.01)
        d0 = [s.keep(k) for k in keys]
        d1 = [HashSampler(99, 0.01).keep(k) for k in keys]
        self.assertEqual(d0, d1)
        # n=1 小样本时允许 0 命中；rate=1 时必命中
        self.assertTrue(HashSampler(99, 1.0).keep("only-one"))

    def test_nested_sampling(self):
        # 低采样率的样本必须是高采样率样本的子集（同 seed 同阈值体系）
        lo = HashSampler(5, 0.01)
        hi = HashSampler(5, 0.1)
        keys = [f"n-{i}" for i in range(50000)]
        lo_set = {k for k in keys if lo.keep(k)}
        hi_set = {k for k in keys if hi.keep(k)}
        self.assertTrue(lo_set <= hi_set)

    def test_bytes_and_str_same_key(self):
        s1 = HashSampler(42, 0.5)
        s2 = HashSampler(42, 0.5)
        self.assertEqual(s1.keep("abc"), s2.keep(b"abc"))


class TestOrderAndShards(unittest.TestCase):
    def test_order_independence(self):
        keys = [f"item-{i}" for i in range(20000)]
        s = HashSampler(2024, 0.05)
        want = {k for k in keys if s.keep(k)}
        shuffled = keys[:]
        random.Random(123).shuffle(shuffled)
        got = {k for k in shuffled if HashSampler(2024, 0.05).keep(k)}
        self.assertEqual(want, got)

    def test_shard_union_equals_whole(self):
        keys = [f"shard-key-{i}" for i in range(60000)]
        whole = {k for k in keys if HashSampler(77, 0.03).keep(k)}
        shards = [keys[:20000], keys[20000:40000], keys[40000:]]
        union = set()
        for part in shards:
            # 每个“分片”用与单机完全相同的 (seed, rate)
            union.update(k for k in part if HashSampler(77, 0.03).keep(k))
        self.assertEqual(union, whole)

    def test_shard_rates_consistent(self):
        # 每个分片命中率都应在 Chernoff 界内
        p, per = 0.02, 100000
        bound = chernoff_abs_bound(per, p, None)
        for shard_id in range(4):
            s = HashSampler("shard", p)
            cnt = sum(s.keep(f"id-{shard_id}-{i}") for i in range(per))
            self.assertLess(abs(cnt - per * p), bound)


class TestDuplicateKeys(unittest.TestCase):
    def test_duplicates_all_or_none(self):
        # 重复键：同一键的所有副本必须同进同出（这是键哈希采样的定义性质）
        s = HashSampler(321, 0.2)
        base = [f"dup-{i % 500}" for i in range(100000)]  # 只有 500 个不同键
        per_key = {}
        for k in base:
            per_key.setdefault(k, s.keep(k))
            self.assertEqual(per_key[k], s.keep(k))

    def test_duplicate_rate_on_distinct_keys(self):
        # 重复行放大命中数，但对“不同键”的采样率仍准确
        distinct = 2000
        reps = 50
        p = 0.05
        s = HashSampler(555, p)
        kept_distinct = sum(s.keep(f"d-{i}") for i in range(distinct))
        bound = chernoff_abs_bound(distinct, p, None)
        self.assertLess(abs(kept_distinct - distinct * p), bound)
        # 行级命中率 = 命中的不同键贡献 reps 行，因此整数倍放大
        kept_rows = sum(s.keep(f"d-{i}") for i in range(distinct) for _ in range(reps))
        self.assertEqual(kept_rows, kept_distinct * reps)


class TestStreaming(unittest.TestCase):
    def test_filter_lazy(self):
        s = HashSampler(8, 0.1)
        result = list(s.filter(iter([f"f-{i}" for i in range(10000)])))
        expect = [f"f-{i}" for i in range(10000) if HashSampler(8, 0.1).keep(f"f-{i}")]
        self.assertEqual(result, expect)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""
test_hashagg.py —— hashagg 自测（unittest，仅标准库）

覆盖：
  1. 单分组
  2. 均匀多分组（强制溢出，对拍朴素 dict）
  3. 极端倾斜（Zipf 样，输出分区大小分布）
  4. 单行超大分组（一个 key 占全部行，内存 O(1)）
  5. 递归溢出收敛（嵌套深度 + 最终分区数）
  6. 同一 key 绝不落入两个分区（直接检查溢出文件）
  7. 随机对拍（多种子，count/sum/min/max 全算子）
  8. 溢出前后结果一致（同数据不同预算）
运行：python3 -m unittest -v test_hashagg
"""
import random
import statistics
import unittest

import hashagg
from hashagg import SpillAggregator

OPS = [("count", None), ("sum", 1), ("min", 2), ("max", 2)]


def naive(rows):
    """参考实现：纯内存 dict 聚合。"""
    out = {}
    for row in rows:
        key, a, b = row
        c, s, mi, ma = out.get(key, (0, 0, None, None))
        out[key] = (c + 1, s + a,
                    b if mi is None else min(mi, b),
                    b if ma is None else max(ma, b))
    return out


def run(rows, budget, **kw):
    with SpillAggregator(OPS, budget=budget, **kw) as agg:
        res = agg.collect(rows)
        stats = dict(agg.stats)
    return res, stats


def gen_uniform(n_rows, n_keys, seed):
    rng = random.Random(seed)
    return [(rng.randrange(n_keys), rng.randint(-10**6, 10**6),
             rng.randint(-10**6, 10**6)) for _ in range(n_rows)]


def gen_zipf(n_rows, n_keys, seed, hot_share=0.5):
    """hot key 0 占 hot_share，其余 key 按 1/rank 几何衰减。"""
    rng = random.Random(seed)
    rows = []
    for _ in range(n_rows):
        r = rng.random()
        if r < hot_share:
            key = 0
        else:
            key = 1 + int(rng.expovariate(1.0 / (n_keys / 8.0))) % (n_keys - 1)
        rows.append((key, rng.randint(-10**6, 10**6), rng.randint(-10**6, 10**6)))
    return rows


class TestHashAgg(unittest.TestCase):

    def test_1_single_group(self):
        rows = [(7, i, -i) for i in range(200_000)]
        res, stats = run(rows, budget=64 * 1024)
        self.assertEqual(res, naive(rows))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[7][0], 200_000)
        print("\n[单分组] 20 万行同一 key: spills=%d (单 key 只占 1 个表项, 无需溢出)"
              % len(stats["spills"]))

    def test_2_uniform_many_groups(self):
        rows = gen_uniform(120_000, 40_000, seed=42)
        res, stats = run(rows, budget=256 * 1024)
        self.assertEqual(res, naive(rows))
        self.assertGreater(len(stats["spills"]), 0)
        print("\n[均匀多组] 12 万行/4 万组, 预算 256KB: spills=%d, "
              "最大深度=%d, 叶子分区=%d"
              % (len(stats["spills"]), stats["max_depth"],
                 stats["leaf_partitions"]))

    def test_3_extreme_skew(self):
        n_rows, n_keys = 300_000, 20_000
        rows = gen_zipf(n_rows, n_keys, seed=7, hot_share=0.6)
        res, stats = run(rows, budget=512 * 1024)
        self.assertEqual(res, naive(rows))
        ev = stats["spills"][0]
        fanout = ev["fanout"]
        mask = fanout - 1
        # 原始行视角：若不做任何预处理，按 pid 直接分区，每分区的原始行数
        raw = [0] * fanout
        for k, _, _ in rows:
            raw[hashagg.partition_id(k, 0, mask)] += 1
        raw.sort(reverse=True)
        dist = sorted(ev["rows_per_partition"], reverse=True)
        med_raw = statistics.median(raw)
        med = statistics.median(dist)
        print("\n[极端倾斜] 30 万行 Zipf(hot=60%%), 2 万组, 预算 512KB")
        print("  首次溢出 fanout=%d" % fanout)
        print("  原始行分区: max=%d p50=%d min=%d, max/p50=%.1f "
              "(hot key 占 60%%, 必须同区, 原始字节天然倾斜)"
              % (raw[0], med_raw, raw[-1], raw[0] / max(1, med_raw)))
        print("  落盘条数分区(热点缓存就地聚合后): max=%d p50=%d min=%d, "
              "max/p50=%.1f" % (dist[0], med, dist[-1], dist[0] / max(1, med)))
        print("  落盘条数分布(降序前 8): %s" % (dist[:8],))
        print("  -> 热点 key 流内预聚合, 落盘量随 distinct key 数而非行数增长; "
              "各分区哈希表均能装入预算")
        # 预聚合后各分区落盘条数应基本均衡
        self.assertLess(dist[0] / max(1, med), 3.0)
        # 原始行必然有一个分区承接 hot key 的全部行（约 60%）
        self.assertGreater(raw[0], 0.55 * n_rows)

    def test_4_giant_single_group(self):
        n = 2_000_000
        rows = ((0, 1, i % 1000) for i in range(n))
        res, stats = run(rows, budget=256 * 1024)
        self.assertEqual(res, {0: (n, n, 0, 999)})
        self.assertEqual(len(stats["spills"]), 0)
        print("\n[单行超大分组] 200 万行同一 key, 预算 256KB: "
              "spills=%d (聚合状态 O(1), 与行数无关)" % len(stats["spills"]))

    def test_5_recursive_spill_converges(self):
        rows = gen_uniform(400_000, 300_000, seed=99)
        res, stats = run(rows, budget=64 * 1024, min_fanout=4)
        self.assertEqual(res, naive(rows))
        self.assertGreaterEqual(stats["max_depth"], 2)
        self.assertLessEqual(stats["max_depth"], 6)
        by_level = {}
        for e in stats["spills"]:
            by_level.setdefault(e["depth"], e["fanout"])
        print("\n[递归收敛] 40 万行/30 万组, 预算 64KB:")
        print("  溢出事件=%d, 各层 fanout=%s" % (len(stats["spills"]),
              dict(sorted(by_level.items()))))
        print("  最大嵌套深度=%d, 最终叶子分区数=%d"
              % (stats["max_depth"], stats["leaf_partitions"]))
        print("  落盘总量=%.1f MB" % (stats["spill_bytes"] / 1e6,))

    def test_6_same_key_never_two_partitions(self):
        """直接写/读溢出文件，验证 key 集合跨分区两两不相交且并集完整。"""
        import tempfile
        rng = random.Random(1)
        rows = [(rng.randrange(50_000), rng.randint(0, 9), rng.randint(0, 9))
                for _ in range(200_000)]
        with SpillAggregator(OPS, budget=128 * 1024) as agg:
            fanout = 16
            mask = fanout - 1
            files = [hashagg._SpillFile(agg.spill_dir, 0, i)
                     for i in range(fanout)]
            for r in rows:
                files[hashagg.partition_id(r[0], 0, mask)].write(
                    r[0], agg._row_state(r))
            for f in files:
                f.close()
            seen = {}
            for i, f in enumerate(files):
                for k, _ in f.read():
                    if k in seen and seen[k] != i:
                        self.fail("key %r 同时落入分区 %d 和 %d"
                                  % (k, seen[k], i))
                    seen[k] = i
                f.unlink()
            self.assertEqual(len(seen), len({r[0] for r in rows}))
            res = agg.collect(rows)
        self.assertEqual(res, naive(rows))
        print("\n[分区唯一性] 20 万行直接落 16 个溢出文件回读: "
              "key 集合两两不相交、并集完整, 聚合结果对拍一致")

    def test_7_differential_random(self):
        for seed in (1, 2, 3, 4, 5):
            rng = random.Random(seed)
            n_keys = rng.choice([1, 10, 1000, 30000])
            rows = gen_uniform(rng.randint(1, 60_000), n_keys, seed)
            budget = rng.choice([32 * 1024, 128 * 1024, 1 << 20])
            res, _ = run(rows, budget=budget)
            self.assertEqual(res, naive(rows), "seed=%d" % seed)
        print("\n[随机对拍] 5 个种子 x 随机行数/组数/预算, 全部与朴素实现一致")

    def test_8_same_result_before_after_spill(self):
        rows = gen_zipf(150_000, 25_000, seed=5, hot_share=0.4)
        big, s1 = run(rows, budget=1 << 30)          # 不溢出
        small, s2 = run(rows, budget=64 * 1024)      # 多级溢出
        self.assertEqual(big, small)
        self.assertEqual(len(s1["spills"]), 0)
        self.assertGreater(len(s2["spills"]), 0)
        print("\n[溢出前后一致] 不溢出 vs 预算 64KB 多级溢出, 结果完全相同 "
              "(spills: %d vs %d)" % (len(s1["spills"]), len(s2["spills"])))


if __name__ == "__main__":
    unittest.main(verbosity=2)

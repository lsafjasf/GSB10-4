"""覆盖索引 vs 回表 代价模型的自测（unittest，仅标准库）。

覆盖情形：
1. 覆盖索引足够          -> 必须选 index_covering，且实测最优
2. 必须回表（低选择率）  -> 选 index_lookup，且与实测最优一致
3. 必须回表（高选择率）  -> 选 full_scan（回表随机 I/O 爆炸）
4. 选择率极端（0 行 / 全表）
5. 统计信息缺失          -> 显式说明 + 保守计划
6. 排序要求              -> ORDER BY 走索引免排序
7. 反馈修正              -> 修正后平均偏差显著下降
"""

import random
import unittest

from cost_model import (FULL_SCAN, INDEX_COVERING, INDEX_LOOKUP,
                        CostModel, Engine, Query, calibrate)

ROWS = 100_000
DISTINCT = 10_000
ROW_WIDTH = 200


def make_engine(seed=1, rows=ROWS):
    rng = random.Random(seed)
    data = [(rng.randrange(DISTINCT), rng.random(), "x" * (ROW_WIDTH - 64))
            for _ in range(rows)]
    return Engine(data, ROW_WIDTH)


def dev(est, act):
    return abs(est - act) / max(act, 100.0)


class CostModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = make_engine()
        cls.stats = cls.eng.build_stats()
        cls.model = CostModel()

    def actual_best(self, q):
        return min(self.model.feasible_plans(q),
                   key=lambda p: self.eng.run(p, q).time_us)

    # 1. 覆盖索引足够
    def test_covering_index_sufficient(self):
        q = Query(lo=1000, hi=1100, covering=True, proj_width=24, name="cov")
        dec = self.model.choose(q, self.stats)
        self.assertEqual(dec.plan, INDEX_COVERING)
        self.assertLess(dec.estimates[INDEX_COVERING].est_us,
                        dec.estimates[FULL_SCAN].est_us)
        self.assertEqual(self.actual_best(q), INDEX_COVERING)

    # 2. 必须回表：低选择率 -> 回表划算
    def test_lookup_low_selectivity(self):
        q = Query(lo=5000, hi=5002, covering=False, proj_width=ROW_WIDTH)
        dec = self.model.choose(q, self.stats)
        self.assertEqual(dec.plan, INDEX_LOOKUP)
        best = self.actual_best(q)
        chosen = self.eng.run(dec.plan, q).time_us
        self.assertLessEqual(chosen, 1.5 * self.eng.run(best, q).time_us)

    # 3. 必须回表：高选择率 -> 全表扫描划算
    def test_lookup_high_selectivity(self):
        q = Query(lo=0, hi=6000, covering=False, proj_width=ROW_WIDTH)
        dec = self.model.choose(q, self.stats)
        self.assertEqual(dec.plan, FULL_SCAN)
        best = self.actual_best(q)
        chosen = self.eng.run(dec.plan, q).time_us
        self.assertLessEqual(chosen, 1.5 * self.eng.run(best, q).time_us)

    # 4a. 选择率极端：空范围
    def test_extreme_selectivity_empty(self):
        q = Query(lo=999_999, hi=1_000_000, covering=True, proj_width=24)
        dec = self.model.choose(q, self.stats)
        self.assertEqual(dec.plan, INDEX_COVERING)
        res = self.eng.run(dec.plan, q)
        self.assertEqual(res.out_count, 0)
        self.assertLess(res.time_us, 50_000)   # 空结果必须极快

    # 4b. 选择率极端：全表范围
    def test_extreme_selectivity_full(self):
        q = Query(lo=-1, hi=DISTINCT + 1, covering=False, proj_width=ROW_WIDTH)
        dec = self.model.choose(q, self.stats)
        self.assertEqual(dec.plan, FULL_SCAN)
        res = self.eng.run(dec.plan, q)
        self.assertEqual(res.out_count, ROWS)

    # 5. 统计信息缺失 -> 显式说明 + 保守计划
    def test_missing_stats_conservative(self):
        q_cov = Query(lo=0, hi=10, covering=True)
        dec = self.model.choose(q_cov, None)
        self.assertFalse(dec.estimated)
        self.assertTrue(dec.reason)                 # 必须显式说明
        self.assertEqual(dec.plan, INDEX_COVERING)  # 保守：能覆盖就覆盖
        q_nc = Query(lo=0, hi=10, covering=False)
        dec = self.model.choose(q_nc, None)
        self.assertFalse(dec.estimated)
        self.assertTrue(dec.reason)
        self.assertEqual(dec.plan, FULL_SCAN)       # 保守：不赌回表

    # 6. 排序要求：ORDER BY 索引列时索引计划免排序
    def test_order_by_prefers_index(self):
        q = Query(lo=0, hi=500, covering=False, order_by_index=True,
                  proj_width=ROW_WIDTH)
        dec = self.model.choose(q, self.stats)
        self.assertEqual(dec.plan, INDEX_LOOKUP)    # 索引序天然有序
        self.assertGreater(dec.estimates[FULL_SCAN].features["sort_nlogn"], 0)
        self.assertEqual(dec.estimates[INDEX_LOOKUP].features["sort_nlogn"], 0)
        res = self.eng.run(dec.plan, q)
        self.assertGreater(res.out_count, 0)

    # 7. 反馈修正：修正后平均偏差显著下降
    def test_calibration_reduces_deviation(self):
        rng = random.Random(7)
        samples, queries, records = [], [], []
        for _ in range(30):
            w = 10 ** rng.uniform(1, 4)
            lo = rng.uniform(0, DISTINCT - min(w, DISTINCT - 1))
            covering = rng.random() < 0.4
            q = Query(lo=lo, hi=lo + w, covering=covering,
                      proj_width=24 if covering else ROW_WIDTH)
            dec = self.model.choose(q, self.stats)
            res = self.eng.run(dec.plan, q)
            samples.append((res.features, res.time_us))
            queries.append(q)
            records.append((dec, res))
        before = [dev(d.estimates[d.plan].est_us, r.time_us)
                  for d, r in records]
        tuned = CostModel(calibrate(samples))
        after = []
        for q, (dec, res) in zip(queries, records):
            dec2 = tuned.choose(q, self.stats)
            after.append(dev(dec2.estimates[dec2.plan].est_us, res.time_us))
        mean = lambda xs: sum(xs) / len(xs)
        self.assertLess(mean(after), mean(before))


if __name__ == "__main__":
    unittest.main(verbosity=2)

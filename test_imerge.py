"""imerge 自测：规则表单测 + 随机对拍（快速实现 vs 逐对参考实现）。

运行：python3 test_imerge.py -v
"""

import random
import unittest
from datetime import datetime, timedelta, timezone

import imerge
from imerge import (
    merge_datetime_intervals,
    merge_intervals,
    merge_intervals_reference,
    normalize_datetime_endpoint,
    normalize_intervals,
    normalize_pairs,
)


class TestBasicRules(unittest.TestCase):
    """规则表 R1-R8 的逐条核对。"""

    def test_r3_disjoint(self):
        # 互不相接：恒不合并
        self.assertEqual(merge_intervals([(0, 1), (2, 3), (5, 7)]),
                         [(0, 1), (2, 3), (5, 7)])
        self.assertEqual(merge_intervals([(0, 1), (2, 3)], merge_touching=True),
                         [(0, 1), (2, 3)])

    def test_r2_touching(self):
        # 恰好相接：默认不合并；merge_touching=True 时合并
        self.assertEqual(merge_intervals([(1, 2), (2, 3)]),
                         [(1, 2), (2, 3)])
        self.assertEqual(merge_intervals([(1, 2), (2, 3)], merge_touching=True),
                         [(1, 3)])
        # 链式相接
        self.assertEqual(merge_intervals([(0, 1), (1, 2), (2, 3)], merge_touching=True),
                         [(0, 3)])

    def test_r1_overlap(self):
        # 重叠：两种策略都合并
        for mt in (False, True):
            self.assertEqual(merge_intervals([(1, 3), (2, 4)], merge_touching=mt),
                             [(1, 4)])

    def test_r4_containment(self):
        # 完全包含：并入包含者，取 min(start)/max(end)
        for mt in (False, True):
            self.assertEqual(merge_intervals([(0, 10), (2, 5), (3, 4)], merge_touching=mt),
                             [(0, 10)])
            # 包含者后出现也一样
            self.assertEqual(merge_intervals([(2, 5), (0, 10)], merge_touching=mt),
                             [(0, 10)])

    def test_r5_point_inside(self):
        # 零长度点落在区间内部：被吸收
        self.assertEqual(merge_intervals([(0, 10), (3, 3)]), [(0, 10)])

    def test_r6_point_at_left_edge(self):
        # 点恰在左端点：被吸收（半开区间包含左端点）
        self.assertEqual(merge_intervals([(0, 10), (0, 0)]), [(0, 10)])

    def test_r7_point_at_right_edge(self):
        # 点恰在右端点：不吸收，点保留（两种策略相同）
        for mt in (False, True):
            self.assertEqual(merge_intervals([(0, 10), (10, 10)], merge_touching=mt),
                             [(0, 10), (10, 10)])

    def test_points_outside_and_duplicates(self):
        # 区间外的点保留；相同的点合并为一个
        self.assertEqual(merge_intervals([(0, 1), (5, 5), (5, 5), (2, 2)]),
                         [(0, 1), (2, 2), (5, 5)])

    def test_point_blocks_touching_merge(self):
        # 点不参与 R2 相接合并：[1,2) 与 [2,3) 之间的点 (2,2) 不搭桥
        self.assertEqual(merge_intervals([(1, 2), (2, 2), (2, 3)], merge_touching=True),
                         [(1, 3)])  # 注意：点被 [1,3) 形成前的区间吸收？见下
        # 上面其实先由 R2 合并 [1,2)+[2,3)=[1,3)，点 (2,2) 落在内部被 R5 吸收

    def test_point_between_untouching(self):
        # 不合并相接时，点 (2,2) 恰是两个区间的端点：属于左区间右端（R7 不吸收），
        # 属于右区间左端（R6 吸收）-> 被 [2,3) 吸收
        self.assertEqual(merge_intervals([(1, 2), (2, 2), (2, 3)]),
                         [(1, 2), (2, 3)])

    def test_empty_and_single(self):
        self.assertEqual(merge_intervals([]), [])
        self.assertEqual(merge_intervals([(4, 4)]), [(4, 4)])
        self.assertEqual(merge_intervals([(4, 9)]), [(4, 9)])

    def test_negative_strict(self):
        with self.assertRaises(ValueError):
            merge_intervals([(5, 2)])
        with self.assertRaises(ValueError):
            normalize_pairs([(5, 2)], negative_policy="strict")

    def test_negative_normalize(self):
        # 负长度翻转为正长度；翻转后为零的降级为零长度点
        cleaned, report = normalize_pairs([(5, 2), (7, 7), (9, 9)], negative_policy="normalize")
        self.assertEqual(cleaned, [(2, 5), (7, 7), (9, 9)])
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["action"], "flipped")
        # (3,3) 不是负长度，不进报告；真正的零长度翻转场景：
        cleaned2, report2 = normalize_pairs([(4, 4), (6, 1)], negative_policy="normalize")
        self.assertEqual(cleaned2, [(4, 4), (1, 6)])
        self.assertEqual(len(report2), 1)


class TestDatetimeNormalization(unittest.TestCase):
    """时区与精度统一规则 T1-T3。"""

    def test_t1_timezone_unify(self):
        a = normalize_datetime_endpoint("2026-01-01T08:00:00+08:00")
        b = normalize_datetime_endpoint("2026-01-01T00:00:00Z")
        self.assertEqual(a.dt, b.dt)  # 同一时刻，统一后相等
        self.assertEqual(a.dt.tzinfo, imerge.UTC)

    def test_t2_naive_assumed_utc(self):
        info = normalize_datetime_endpoint("2026-01-01T00:00:00")
        self.assertTrue(info.tz_assumed)
        self.assertEqual(info.dt, datetime(2026, 1, 1, tzinfo=imerge.UTC))
        with self.assertRaises(ValueError):
            normalize_datetime_endpoint("2026-01-01T00:00:00", naive_policy="reject")

    def test_t3_sub_micro_truncated(self):
        # 纳秒字符串：亚微秒部分截断，不四舍五入
        info = normalize_datetime_endpoint("2026-01-01T00:00:00.123456789Z")
        self.assertEqual(info.dt.microsecond, 123456)
        self.assertEqual(info.sub_micro, "789")
        # float epoch 的亚微秒尾数同样截断
        info2 = normalize_datetime_endpoint(1767225600.123456789)
        self.assertEqual(info2.dt.microsecond, 123456)
        self.assertTrue(info2.sub_micro)

    def test_epoch_and_datetime_inputs(self):
        self.assertEqual(normalize_datetime_endpoint(0).dt,
                         datetime(1970, 1, 1, tzinfo=imerge.UTC))
        aware = datetime(2026, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(normalize_datetime_endpoint(aware).dt,
                         datetime(2026, 1, 1, 0, 0, tzinfo=imerge.UTC))

    def test_pipeline_merges_across_timezones(self):
        # 08:00~09:00 (+08:00) 与 00:30~01:00 (UTC) 统一后重叠 -> 合并
        r = merge_datetime_intervals([
            ("2026-01-01T08:00:00+08:00", "2026-01-01T09:00:00+08:00"),
            ("2026-01-01T00:30:00Z", "2026-01-01T01:00:00Z"),
        ])
        self.assertEqual(r["merged_iso"],
                         [["2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00"]])

    def test_pipeline_negative(self):
        with self.assertRaises(ValueError):
            merge_datetime_intervals([("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z")])
        r = merge_datetime_intervals(
            [("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z")],
            negative_policy="normalize")
        self.assertEqual(r["merged_iso"],
                         [["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"]])
        self.assertEqual(r["stats"]["negative_flipped"], 1)


class TestCrossCheck(unittest.TestCase):
    """随机对拍：快速实现必须与逐对两两合并的参考实现完全一致。"""

    def _random_batch(self, rng, n, allow_negative):
        pairs = []
        for _ in range(n):
            a = rng.randint(0, 50)
            b = rng.randint(0, 50)
            if not allow_negative and b < a:
                a, b = b, a
            pairs.append((a, b))
        return pairs

    def test_random_crosscheck(self):
        rng = random.Random(20261005)
        cases = 0
        for _ in range(400):
            n = rng.randint(0, 12)
            pairs = self._random_batch(rng, n, allow_negative=False)
            for mt in (False, True):
                fast = merge_intervals(pairs, merge_touching=mt)
                ref = merge_intervals_reference(pairs, merge_touching=mt)
                self.assertEqual(fast, ref, "pairs=%r mt=%r" % (pairs, mt))
                cases += 1
        # 负长度：normalize 后再对拍
        for _ in range(100):
            n = rng.randint(1, 12)
            pairs = self._random_batch(rng, n, allow_negative=True)
            cleaned, _ = normalize_pairs(pairs, negative_policy="normalize")
            for mt in (False, True):
                self.assertEqual(merge_intervals(cleaned, merge_touching=mt),
                                 merge_intervals_reference(cleaned, merge_touching=mt))
                cases += 1
        print("\n随机对拍通过：%d 组（含零长度/相接/包含/负长度翻转）" % cases)

    def test_reference_is_order_insensitive(self):
        # 同一批数据任意打乱输入顺序，结果必须唯一（回应“同批数据多种结果”）
        rng = random.Random(7)
        pairs = [(rng.randint(0, 20), rng.randint(0, 20)) for _ in range(15)]
        pairs = [(min(a, b), max(a, b)) for a, b in pairs]
        expected = merge_intervals(pairs)
        for _ in range(50):
            shuffled = pairs[:]
            rng.shuffle(shuffled)
            self.assertEqual(merge_intervals(shuffled), expected)
            self.assertEqual(merge_intervals_reference(shuffled), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)

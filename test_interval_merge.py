"""interval_merge 的单元测试与随机对拍（python -m unittest 运行）。"""

import random
import unittest

from interval_merge import (
    Interval,
    NegativeLengthError,
    format_us,
    make_interval,
    merge_inputs,
    merge_pairwise,
    merge_sorted,
    overlaps_or_touches,
    parse_time,
    quantize,
)


class TestBasicMerge(unittest.TestCase):
    def test_disjoint_not_merged(self):
        # 互不相接：[0,10] 与 [20,30] 保持两段
        r = merge_sorted([Interval(0, 10), Interval(20, 30)])
        self.assertEqual(r, [Interval(0, 10), Interval(20, 30)])

    def test_touching_merged(self):
        # 恰好相接：[0,10] 与 [10,20] 合并为 [0,20]（闭区间规则）
        r = merge_sorted([Interval(0, 10), Interval(10, 20)])
        self.assertEqual(r, [Interval(0, 20)])

    def test_contained_absorbed(self):
        # 完全包含：[0,100] 吸收 [10,50]
        r = merge_sorted([Interval(0, 100), Interval(10, 50)])
        self.assertEqual(r, [Interval(0, 100)])

    def test_partial_overlap(self):
        r = merge_sorted([Interval(0, 15), Interval(10, 20)])
        self.assertEqual(r, [Interval(0, 20)])

    def test_unsorted_input(self):
        r = merge_sorted([Interval(20, 30), Interval(0, 10), Interval(5, 25)])
        self.assertEqual(r, [Interval(0, 30)])

    def test_empty(self):
        self.assertEqual(merge_sorted([]), [])


class TestZeroLength(unittest.TestCase):
    def test_zero_length_is_valid_point(self):
        iv = Interval(5, 5)
        self.assertTrue(iv.is_zero_length)
        self.assertEqual(iv.length, 0)

    def test_zero_length_alone_survives(self):
        r = merge_sorted([Interval(5, 5)])
        self.assertEqual(r, [Interval(5, 5)])

    def test_zero_length_inside_interval_absorbed(self):
        # 点 [5,5] 落在 [0,10] 内 -> 被吸收
        r = merge_sorted([Interval(0, 10), Interval(5, 5)])
        self.assertEqual(r, [Interval(0, 10)])

    def test_zero_length_touching_merges(self):
        # 点 [10,10] 与 [0,10] 相接 -> 合并（结果不变，但属同一连通段）
        r = merge_sorted([Interval(0, 10), Interval(10, 10)])
        self.assertEqual(r, [Interval(0, 10)])

    def test_two_same_points_merge(self):
        r = merge_sorted([Interval(7, 7), Interval(7, 7)])
        self.assertEqual(r, [Interval(7, 7)])

    def test_two_distinct_points_stay_separate(self):
        r = merge_sorted([Interval(7, 7), Interval(9, 9)])
        self.assertEqual(r, [Interval(7, 7), Interval(9, 9)])

    def test_point_chain(self):
        # [0,0],[0,5],[5,5] 全部连通 -> [0,5]
        r = merge_sorted([Interval(0, 0), Interval(0, 5), Interval(5, 5)])
        self.assertEqual(r, [Interval(0, 5)])


class TestNegativeLength(unittest.TestCase):
    def test_interval_ctor_rejects_negative(self):
        with self.assertRaises(NegativeLengthError):
            Interval(10, 5)

    def test_default_raise(self):
        with self.assertRaises(NegativeLengthError):
            make_interval(10, 5, unit="us")

    def test_drop_policy(self):
        rep = merge_inputs([(0, 10), (20, 5), (30, 40)], unit="us", on_negative="drop")
        self.assertEqual(rep.merged, [Interval(0, 10), Interval(30, 40)])
        self.assertEqual(rep.dropped_negative, [(20, 5)])
        self.assertEqual(rep.swapped_negative, [])

    def test_swap_policy(self):
        rep = merge_inputs([(0, 10), (20, 5)], unit="us", on_negative="swap")
        self.assertEqual(rep.merged, [Interval(0, 20)])
        self.assertEqual(rep.swapped_negative, [(20, 5)])

    def test_zero_vs_negative_distinguished(self):
        # 零长度合法、负长度非法：两者必须走不同路径
        self.assertEqual(make_interval(5, 5, unit="us"), Interval(5, 5))
        with self.assertRaises(NegativeLengthError):
            make_interval(5, 4, unit="us")


class TestTimezoneAndPrecision(unittest.TestCase):
    def test_same_instant_different_offsets(self):
        a = parse_time("2026-01-01T08:00:00+08:00")
        b = parse_time("2026-01-01T00:00:00Z")
        self.assertEqual(a, b)

    def test_naive_defaults_to_utc(self):
        a = parse_time("2026-01-01T00:00:00")
        b = parse_time("2026-01-01T00:00:00Z")
        self.assertEqual(a, b)

    def test_units(self):
        self.assertEqual(parse_time(1, unit="s"), 1_000_000)
        self.assertEqual(parse_time(1, unit="ms"), 1_000)
        self.assertEqual(parse_time(1, unit="us"), 1)

    def test_quantize_widens_not_loses(self):
        iv = Interval(1_500_000, 2_500_000)
        q = quantize(iv, 1_000_000)
        self.assertEqual(q, Interval(1_000_000, 3_000_000))
        self.assertLessEqual(q.start, iv.start)
        self.assertGreaterEqual(q.end, iv.end)

    def test_quantize_makes_touching_merge(self):
        # 原始不相接（差 1us），对齐到秒后相接 -> 合并
        a = Interval(0, 1_000_000 - 1)
        b = Interval(1_000_000, 2_000_000)
        self.assertEqual(len(merge_sorted([a, b])), 2)
        qa, qb = quantize(a, 1_000_000), quantize(b, 1_000_000)
        self.assertEqual(merge_sorted([qa, qb]), [Interval(0, 2_000_000)])

    def test_mixed_tz_merge(self):
        # 同一物理时刻用不同时区表示，合并结果应一致
        rep = merge_inputs([
            ("2026-01-01T08:00:00+08:00", "2026-01-01T09:00:00+08:00"),
            ("2026-01-01T00:30:00Z", "2026-01-01T01:30:00Z"),
        ])
        self.assertEqual(len(rep.merged), 1)
        self.assertEqual(format_us(rep.merged[0].start), "2026-01-01T00:00:00Z")
        self.assertEqual(format_us(rep.merged[0].end), "2026-01-01T01:30:00Z")


class TestPairwiseCrossCheck(unittest.TestCase):
    """排序合并与逐对两两合并参考实现对拍。"""

    def check_case(self, ivs):
        fast = merge_sorted(ivs)
        ref = merge_pairwise(ivs)
        self.assertEqual(fast, ref, f"不一致: {ivs}")
        # 结果不变量：有序、相邻严格分离
        for prev, nxt in zip(fast, fast[1:]):
            self.assertLess(prev.end, nxt.start)

    def test_handcrafted(self):
        cases = [
            [],
            [Interval(0, 0)],
            [Interval(0, 10), Interval(10, 20), Interval(20, 30)],
            [Interval(0, 100), Interval(10, 20), Interval(30, 40)],
            [Interval(5, 5), Interval(0, 5), Interval(5, 10)],
            [Interval(0, 1), Interval(3, 4), Interval(6, 7)],
        ]
        for c in cases:
            self.check_case(c)

    def test_random_cross_check(self):
        rng = random.Random(20261005)
        for trial in range(500):
            n = rng.randint(0, 30)
            ivs = []
            for _ in range(n):
                a = rng.randint(0, 100)
                b = a + rng.randint(0, 20)  # 含零长度
                ivs.append(Interval(a, b))
            self.check_case(ivs)


if __name__ == "__main__":
    unittest.main(verbosity=2)

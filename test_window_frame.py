"""test_window_frame.py — window_frame 自测（仅标准库 unittest）。

覆盖：
- 手工推算逐行对拍（含并列值、空帧、全帧、单行、多分区）
- 快速实现 vs 朴素参照实现 随机对拍
- 排序键不稳定时的可复现性
运行：python3 test_window_frame.py -v
"""

import random
import unittest

from window_frame import (
    UNBOUNDED, CURRENT_ROW, PRECEDING, FOLLOWING,
    Frame, DEFAULT_FRAME, EMPTY_AGG,
    frame_bounds, evaluate, evaluate_naive,
)

UB, CR, P, F = UNBOUNDED, CURRENT_ROW, PRECEDING, FOLLOWING


def rows_frame(a, b):
    return Frame("ROWS", a, b)


def range_frame(a, b):
    return Frame("RANGE", a, b)


def groups_frame(a, b):
    return Frame("GROUPS", a, b)


# ---------------------------------------------------------------------------
# 手工推算数据集
# A: 单分区、无排序键（按输入顺序），值 [10, 20, 20, 30, 40]
# B: 单分区、排序键含并列，(k, v) = (1,10),(2,20),(2,25),(3,30),(5,40)
# ---------------------------------------------------------------------------
DATA_A = [10, 20, 20, 30, 40]
DATA_B = [(1, 10), (2, 20), (2, 25), (3, 30), (5, 40)]
KEY_B = lambda r: r[0]
VAL_B = lambda r: r[1]


class TestHandComputed(unittest.TestCase):
    """每个期望值均为手工推算，见 SEMANTICS.md 的逐行对拍表。"""

    def test_a_rows_unbounded_preceding_to_current(self):
        # 帧 = 第0行..当前行（截断并列：与值无关，纯物理行）
        got = evaluate(DATA_A, rows_frame((UB, None), (CR, None)), "sum")
        self.assertEqual(got, [10, 30, 50, 80, 120])

    def test_a_rows_1p_1f(self):
        frame = rows_frame((P, 1), (F, 1))
        self.assertEqual(evaluate(DATA_A, frame, "sum"), [30, 50, 70, 90, 70])
        self.assertEqual(evaluate(DATA_A, frame, "count"), [2, 3, 3, 3, 2])
        self.assertEqual(evaluate(DATA_A, frame, "min"), [10, 10, 20, 20, 30])
        self.assertEqual(evaluate(DATA_A, frame, "max"), [20, 20, 30, 40, 40])
        self.assertEqual(evaluate(DATA_A, frame, "first_value"), [10, 10, 20, 20, 30])
        self.assertEqual(evaluate(DATA_A, frame, "last_value"), [20, 20, 30, 40, 40])
        got_avg = evaluate(DATA_A, frame, "avg")
        exp_avg = [15.0, 50 / 3, 70 / 3, 30.0, 35.0]
        for g, e in zip(got_avg, exp_avg):
            self.assertAlmostEqual(g, e)

    def test_a_rows_empty_frame(self):
        # ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING：第 0 行为空帧
        frame = rows_frame((P, 1), (P, 1))
        self.assertEqual(evaluate(DATA_A, frame, "sum"), [None, 10, 20, 20, 30])
        self.assertEqual(evaluate(DATA_A, frame, "count"), [0, 1, 1, 1, 1])
        self.assertEqual(evaluate(DATA_A, frame, "min"), [None, 10, 20, 20, 30])

    def test_b_range_1p_current_ties_merged(self):
        # 并列值整体并入：k=2 的两行帧完全相同 [0,2]
        frame = range_frame((P, 1), (CR, None))
        kw = dict(order_key=KEY_B, value=VAL_B)
        self.assertEqual(evaluate(DATA_B, frame, "sum", **kw), [10, 55, 55, 75, 40])
        self.assertEqual(evaluate(DATA_B, frame, "count", **kw), [1, 3, 3, 3, 1])
        self.assertEqual(evaluate(DATA_B, frame, "min", **kw), [10, 10, 10, 20, 40])
        self.assertEqual(evaluate(DATA_B, frame, "max", **kw), [10, 25, 25, 30, 40])

    def test_b_default_frame(self):
        # 默认帧 RANGE UNBOUNDED PRECEDING AND CURRENT ROW：累计值，并列并入
        kw = dict(order_key=KEY_B, value=VAL_B)
        self.assertEqual(evaluate(DATA_B, None, "sum", **kw), [10, 55, 55, 85, 125])
        self.assertEqual(evaluate(DATA_B, DEFAULT_FRAME, "sum", **kw),
                         [10, 55, 55, 85, 125])

    def test_b_range_empty_frame(self):
        # RANGE BETWEEN 1 PRECEDING AND 1 PRECEDING：k=1 与 k=5 行为空帧
        frame = range_frame((P, 1), (P, 1))
        kw = dict(order_key=KEY_B, value=VAL_B)
        self.assertEqual(evaluate(DATA_B, frame, "sum", **kw),
                         [None, 10, 10, 45, None])
        self.assertEqual(evaluate(DATA_B, frame, "count", **kw), [0, 1, 1, 2, 0])
        self.assertEqual(evaluate(DATA_B, frame, "avg", **kw),
                         [None, 10.0, 10.0, 22.5, None])

    def test_b_rows_vs_range_tie_contrast(self):
        # 同一并列数据：ROWS 截断（两行不同），RANGE 并入（两行相同）
        kw = dict(order_key=KEY_B, value=VAL_B)
        rows_got = evaluate(DATA_B, rows_frame((UB, None), (CR, None)), "sum", **kw)
        range_got = evaluate(DATA_B, range_frame((UB, None), (CR, None)), "sum", **kw)
        self.assertEqual(rows_got, [10, 30, 55, 85, 125])   # 截断：逐行累积
        self.assertEqual(range_got, [10, 55, 55, 85, 125])  # 并入：同键同帧

    def test_b_groups_1p_1f(self):
        # 组：{1} {2,2} {3} {5}
        frame = groups_frame((P, 1), (F, 1))
        kw = dict(order_key=KEY_B, value=VAL_B)
        self.assertEqual(evaluate(DATA_B, frame, "sum", **kw),
                         [55, 85, 85, 115, 70])

    def test_full_frame(self):
        # 全帧：每行帧都是整个分区
        frame = rows_frame((UB, None), (UB, None))
        kw = dict(order_key=KEY_B, value=VAL_B)
        self.assertEqual(evaluate(DATA_B, frame, "sum", **kw), [125] * 5)
        self.assertEqual(evaluate(DATA_B, frame, "count", **kw), [5] * 5)
        self.assertEqual(evaluate(DATA_B, frame, "min", **kw), [10] * 5)
        self.assertEqual(evaluate(DATA_B, frame, "max", **kw), [40] * 5)
        self.assertEqual(evaluate(DATA_B, frame, "avg", **kw), [25.0] * 5)

    def test_single_row(self):
        for frame in (rows_frame((UB, None), (UB, None)),
                      rows_frame((P, 3), (F, 3)),
                      range_frame((P, 1), (F, 1)),
                      DEFAULT_FRAME):
            self.assertEqual(evaluate([99], frame, "sum"), [99])
            self.assertEqual(evaluate([99], frame, "count"), [1])
        # 单行空帧
        self.assertEqual(evaluate([99], rows_frame((P, 1), (P, 1)), "sum"), [None])
        self.assertEqual(evaluate([99], rows_frame((P, 1), (P, 1)), "count"), [0])

    def test_multi_partition(self):
        # 两个分区，输入乱序；RANGE 默认帧累计
        rows = [("b", 3, 300), ("a", 1, 10), ("b", 1, 100),
                ("a", 2, 20), ("b", 3, 30)]
        got = evaluate(rows, None, "sum",
                       partition_key=lambda r: r[0],
                       order_key=lambda r: r[1],
                       value=lambda r: r[2])
        # a: 10, 10+20=30 ; b: 100, 100+300+30=430（k=3 并列并入）
        self.assertEqual(got, [430, 10, 100, 30, 430])

    def test_frame_bounds_empty_and_clip(self):
        # 直接检查边界：闭区间、裁剪、空帧
        starts, ends = frame_bounds([1, 2, 2, 3, 5], rows_frame((P, 2), (F, 1)))
        self.assertEqual(starts, [0, 0, 0, 1, 2])
        self.assertEqual(ends, [1, 2, 3, 4, 4])
        starts, ends = frame_bounds([1, 2, 2, 3, 5], range_frame((P, 1), (P, 1)))
        self.assertEqual(starts, [0, 0, 0, 1, 4])   # [0,-1] 与 [4,3] 为空帧
        self.assertEqual(ends, [-1, 0, 0, 2, 3])
        starts, ends = frame_bounds([1, 2, 2, 3, 5], range_frame((P, 1), (CR, None)))
        self.assertEqual(starts, [0, 0, 0, 1, 4])
        self.assertEqual(ends, [0, 2, 2, 3, 4])

    def test_empty_agg_contract(self):
        # 空帧取值契约：count=0，其余 None
        frame = rows_frame((P, 1), (P, 1))
        self.assertEqual(evaluate([7], frame, "count"), [EMPTY_AGG["count"]])
        for agg in ("sum", "avg", "min", "max", "first_value", "last_value"):
            self.assertEqual(evaluate([7], frame, agg), [EMPTY_AGG[agg]])


class TestCrossCheck(unittest.TestCase):
    """快速实现 vs 朴素参照实现：固定种子随机对拍。"""

    def test_random_cross_check(self):
        rng = random.Random(20261006)
        frames = [
            rows_frame((UB, None), (CR, None)),
            rows_frame((UB, None), (UB, None)),
            rows_frame((P, 2), (F, 1)),
            rows_frame((P, 1), (P, 1)),          # 可能空帧
            rows_frame((CR, None), (UB, None)),
            range_frame((UB, None), (CR, None)),
            range_frame((P, 3), (F, 2)),
            range_frame((P, 2), (P, 1)),         # 可能空帧
            range_frame((CR, None), (UB, None)),
            groups_frame((P, 1), (F, 1)),
            groups_frame((P, 2), (P, 1)),        # 可能空帧
            None,                                # 默认帧
        ]
        aggs = ["count", "sum", "avg", "min", "max", "first_value", "last_value"]
        for trial in range(200):
            n = rng.randint(1, 30)
            rows = [(rng.randint(0, 2),           # 分区键（少量 -> 多并列）
                     rng.randint(0, 6),           # 排序键（大量并列）
                     rng.randint(-50, 50))        # 值
                    for _ in range(n)]
            frame = rng.choice(frames)
            agg = rng.choice(aggs)
            kw = dict(partition_key=lambda r: r[0],
                      order_key=lambda r: r[1],
                      value=lambda r: r[2])
            fast = evaluate(rows, frame, agg, **kw)
            slow = evaluate_naive(rows, frame, agg, **kw)
            self.assertEqual(len(fast), n)
            for g, s in zip(fast, slow):
                if isinstance(s, float):
                    self.assertAlmostEqual(g, s, places=9,
                                           msg="trial=%d frame=%r agg=%s" % (trial, frame, agg))
                else:
                    self.assertEqual(g, s,
                                     msg="trial=%d frame=%r agg=%s" % (trial, frame, agg))


class TestReproducibility(unittest.TestCase):
    """排序键不稳定（大量重复）时的可复现性。"""

    def setUp(self):
        rng = random.Random(7)
        self.rows = [(rng.randint(0, 3), rng.randint(0, 5), rng.randint(0, 99))
                     for _ in range(500)]
        self.kw = dict(partition_key=lambda r: r[0],
                       order_key=lambda r: r[1],
                       value=lambda r: r[2])

    def test_repeat_runs_identical(self):
        # 同一输入重复运行 3 次，结果逐元素一致
        frames = [rows_frame((P, 2), (F, 2)),
                  range_frame((P, 2), (CR, None)),
                  groups_frame((P, 1), (F, 1))]
        for frame in frames:
            runs = [evaluate(self.rows, frame, "sum", **self.kw) for _ in range(3)]
            self.assertEqual(runs[0], runs[1])
            self.assertEqual(runs[1], runs[2])

    def test_range_shuffle_invariant(self):
        # RANGE/GROUPS：输入打乱后，输出多重集不变（帧只取决于键值）
        rng = random.Random(99)
        for frame in (range_frame((P, 2), (F, 1)), groups_frame((P, 1), (F, 1))):
            base = evaluate(self.rows, frame, "sum", **self.kw)
            for _ in range(3):
                shuffled = list(self.rows)
                rng.shuffle(shuffled)
                got = evaluate(shuffled, frame, "sum", **self.kw)
                self.assertEqual(sorted(base), sorted(got))

    def test_rows_shuffle_deterministic_per_input(self):
        # ROWS 依赖物理顺序：同一输入多次运行一致（不保证跨打乱一致）
        frame = rows_frame((P, 2), (F, 2))
        rng = random.Random(123)
        shuffled = list(self.rows)
        rng.shuffle(shuffled)
        runs = [evaluate(shuffled, frame, "sum", **self.kw) for _ in range(3)]
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[1], runs[2])


class TestValidation(unittest.TestCase):
    def test_bad_frame(self):
        with self.assertRaises(ValueError):
            evaluate([1, 2], rows_frame((P, -1), (CR, None)), "sum")
        with self.assertRaises(ValueError):
            evaluate([1, 2], Frame("ROWS", ("BAD_KIND", 1), (CR, None)), "sum")
        with self.assertRaises(ValueError):
            evaluate([1, 2], groups_frame((P, 1), (F, 0.5)), "sum")  # GROUPS 需整数
        with self.assertRaises(ValueError):
            evaluate([1, 2], Frame("XXX", (UB, None), (CR, None)), "sum")
        with self.assertRaises(ValueError):
            evaluate([1, 2], None, "median")


if __name__ == "__main__":
    unittest.main(verbosity=2)

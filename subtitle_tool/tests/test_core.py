"""subtitle_tool 单元测试：边界情形 + 手工对拍。"""

import random
import unittest

from subtitle_tool.core import (DROPPED_FULLY_COVERED, OVERLAP_INTRODUCED,
                                START_BEFORE_ZERO, Cue, ShiftViolationError,
                                ShiftWindow, merge_sources, shift_global,
                                shift_segmented)
from subtitle_tool.srtio import format_ms, parse_srt, render_srt


def cue(s, e, text="x", tag=""):
    return Cue(s, e, text, tag)


def iv(cues):
    return [(c.start, c.end, c.text) for c in cues]


class TestMergeBasics(unittest.TestCase):
    def test_empty_sources(self):
        r = merge_sources([[], []])
        self.assertEqual(r.cues, [])
        self.assertEqual(r.reports, [])

    def test_single_source_passthrough(self):
        r = merge_sources([[cue(0, 1000, "a"), cue(2000, 3000, "b")]])
        self.assertEqual(iv(r.cues), [(0, 1000, "a"), (2000, 3000, "b")])

    def test_full_overlap_dropped_with_report(self):
        r = merge_sources([[cue(0, 5000, "long")], [cue(1000, 2000, "inner")]])
        self.assertEqual(iv(r.cues), [(0, 5000, "long")])
        self.assertEqual(len(r.reports), 1)
        self.assertEqual(r.reports[0].kind, DROPPED_FULLY_COVERED)
        self.assertEqual(r.reports[0].cue.text, "inner")

    def test_partial_overlap_clipped(self):
        r = merge_sources([[cue(0, 3000, "a")], [cue(2000, 4000, "b")]])
        self.assertEqual(iv(r.cues), [(0, 3000, "a"), (3000, 4000, "b")])

    def test_same_start_longer_wins(self):
        r = merge_sources([[cue(1000, 2000, "short")], [cue(1000, 4000, "long")]])
        self.assertEqual(iv(r.cues), [(1000, 4000, "long")])
        self.assertEqual(r.reports[0].kind, DROPPED_FULLY_COVERED)

    def test_identical_interval_first_source_wins(self):
        r = merge_sources([[cue(0, 1000, "first")], [cue(0, 1000, "second")]])
        self.assertEqual(iv(r.cues), [(0, 1000, "first")])
        self.assertEqual(r.reports[0].cue.text, "second")

    def test_later_cue_left_clipped_never_split(self):
        # 规则：开始时间早的优先，晚的只从左侧裁剪，不会分裂
        r = merge_sources([
            [cue(0, 2000, "x"), cue(8000, 10000, "y")],
            [cue(1000, 9000, "span")],
        ])
        self.assertEqual(iv(r.cues),
                         [(0, 2000, "x"), (2000, 9000, "span"),
                          (9000, 10000, "y")])

    def test_touching_is_not_overlap(self):
        # 半开区间：end == start 不算重叠
        r = merge_sources([[cue(0, 1000, "a")], [cue(1000, 2000, "b")]])
        self.assertEqual(iv(r.cues), [(0, 1000, "a"), (1000, 2000, "b")])
        self.assertEqual(r.reports, [])

    def test_zero_duration_rejected(self):
        with self.assertRaises(ValueError):
            cue(1000, 1000)
        with self.assertRaises(ValueError):
            cue(2000, 1000)
        with self.assertRaises(ValueError):
            cue(-1, 1000)

    def test_merge_random_invariants(self):
        rng = random.Random(42)
        for _ in range(50):
            srcs = []
            for _ in range(rng.randint(1, 4)):
                srcs.append([cue(s, s + rng.randint(1, 3000))
                             for s in sorted(rng.randrange(0, 20000)
                                             for _ in range(rng.randint(0, 30)))])
            r = merge_sources(srcs)
            for a, b in zip(r.cues, r.cues[1:]):
                self.assertLess(a.start, b.start)
                self.assertLessEqual(a.end, b.start)  # 无重叠无嵌套
            for c in r.cues:
                self.assertGreater(c.end, c.start)    # 无零时长
                self.assertGreaterEqual(c.start, 0)


class TestShiftGlobal(unittest.TestCase):
    def test_positive_offset(self):
        r = shift_global([cue(1000, 2000, "a")], 500)
        self.assertEqual(iv(r.cues), [(1500, 2500, "a")])
        self.assertEqual(r.reports, [])

    def test_negative_offset_crossing_zero_reported_not_clamped(self):
        r = shift_global([cue(500, 1500, "a"), cue(5000, 6000, "b")],
                         -1000, strict=False)
        # 越界条目保持原样（不截断到 0），其余正常偏移
        self.assertEqual(iv(r.cues), [(500, 1500, "a"), (4000, 5000, "b")])
        self.assertEqual(len(r.reports), 1)
        self.assertEqual(r.reports[0].kind, START_BEFORE_ZERO)
        self.assertEqual(r.reports[0].cue.text, "a")

    def test_strict_raises_with_reports(self):
        with self.assertRaises(ShiftViolationError) as ctx:
            shift_global([cue(100, 200)], -500, strict=True)
        self.assertEqual(len(ctx.exception.reports), 1)

    def test_offset_to_exactly_zero_ok(self):
        r = shift_global([cue(1000, 2000, "a")], -1000)
        self.assertEqual(iv(r.cues), [(0, 1000, "a")])


class TestShiftSegmented(unittest.TestCase):
    def test_windows_applied_outside_untouched(self):
        cues = [cue(1000, 2000, "a"), cue(5000, 6000, "b"), cue(9000, 10000, "c")]
        r = shift_segmented(cues, [ShiftWindow(4000, 8000, -1000)])
        self.assertEqual(iv(r.cues),
                         [(1000, 2000, "a"), (4000, 5000, "b"),
                          (9000, 10000, "c")])

    def test_overlapping_windows_rejected(self):
        with self.assertRaises(ValueError):
            shift_segmented([cue(0, 100)],
                            [ShiftWindow(0, 5000, 1), ShiftWindow(4000, 9000, 1)])

    def test_out_of_bounds_in_window(self):
        r = shift_segmented([cue(1000, 2000, "a")],
                            [ShiftWindow(0, 5000, -2000)], strict=False)
        self.assertEqual(iv(r.cues), [(1000, 2000, "a")])  # 保持原样
        self.assertEqual(r.reports[0].kind, START_BEFORE_ZERO)

    def test_overlap_introduced_detected(self):
        # 两条相邻，后一条在窗口内前移 500ms -> 与前一条重叠
        cues = [cue(1000, 2000, "a"), cue(2000, 3000, "b")]
        r = shift_segmented(cues, [ShiftWindow(2000, 5000, -500)])
        self.assertEqual(iv(r.cues), [(1000, 2000, "a"), (1500, 2500, "b")])
        kinds = [rep.kind for rep in r.reports]
        self.assertIn(OVERLAP_INTRODUCED, kinds)

    def test_result_sorted_by_start(self):
        cues = [cue(0, 500, "a"), cue(1000, 1500, "b")]
        r = shift_segmented(cues, [ShiftWindow(0, 600, 2000)])
        self.assertEqual([c.text for c in r.cues], ["b", "a"])


class TestSrtIO(unittest.TestCase):
    def test_format_ms(self):
        self.assertEqual(format_ms(0), "00:00:00,000")
        self.assertEqual(format_ms(3_723_456), "01:02:03,456")

    def test_roundtrip(self):
        text = ("1\n00:00:01,000 --> 00:00:02,500\nhello\nworld\n\n"
                "2\n01:00:00,000 --> 01:00:01,000\nbye\n")
        cues = parse_srt(text, tag="t")
        self.assertEqual(iv(cues), [(1000, 2500, "hello\nworld"),
                                    (3_600_000, 3_601_000, "bye")])
        again = parse_srt(render_srt(cues))
        self.assertEqual(iv(again), iv(cues))

    def test_zero_duration_in_srt_rejected(self):
        with self.assertRaises(ValueError):
            parse_srt("1\n00:00:01,000 --> 00:00:01,000\nbad\n")


if __name__ == "__main__":
    unittest.main()

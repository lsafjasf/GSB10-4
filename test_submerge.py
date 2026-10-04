"""Self-tests for submerge: hand-computed cross-checks, edge cases, benchmark.

Run:  python3 test_submerge.py -v
"""
import os
import random
import time
import tracemalloc
import unittest

import submerge as sm

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def load_cues(name):
    return sm.parse_srt(load(name), src=name)


class TestCrossCheck(unittest.TestCase):
    """Library output must equal the hand-derived fixtures byte for byte."""

    def assert_merge_matches(self, expected_name, *input_names):
        result = sm.merge_cues(*[load_cues(n) for n in input_names])
        self.assertEqual(sm.format_srt(result.cues), load("expected/" + expected_name))
        return result

    def test_simple_overlap_and_touching_boundaries(self):
        self.assert_merge_matches(
            "case_overlap_merged.srt", "case_overlap_a.srt", "case_overlap_b.srt"
        )

    def test_transitive_chain_and_nesting(self):
        self.assert_merge_matches(
            "case_chain_merged.srt", "case_chain_a.srt", "case_chain_b.srt"
        )

    def test_zero_duration_dropped(self):
        result = self.assert_merge_matches("case_zero_merged.srt", "case_zero_a.srt")
        self.assertEqual(len(result.dropped), 1)
        self.assertEqual(result.dropped[0].text, "ZERO")

    def test_empty_inputs(self):
        self.assert_merge_matches("case_empty_merged.srt", "case_empty.srt")
        result = sm.merge_cues([], [])
        self.assertEqual(result.cues, [])
        self.assertEqual(sm.format_srt([]), "")

    def test_uniform_shift(self):
        shifted = sm.shift_cues(load_cues("case_shift_in.srt"), 2500)
        self.assertEqual(sm.format_srt(shifted), load("expected/case_shift_plus2500.srt"))

    def test_segmented_shift(self):
        shifted = sm.shift_by_segments(
            load_cues("case_shift_in.srt"), [sm.Segment(4000, 8000, -3000)]
        )
        self.assertEqual(
            sm.format_srt(shifted), load("expected/case_shift_segments.srt")
        )

    def test_segmented_shift_remerge(self):
        shifted = sm.shift_by_segments(
            load_cues("case_segoverlap_in.srt"), [sm.Segment(2500, 5000, -2000)]
        )
        merged = sm.merge_cues(shifted)
        self.assertEqual(
            sm.format_srt(merged.cues), load("expected/case_segoverlap_merged.srt")
        )


class TestShiftSemantics(unittest.TestCase):
    def test_uniform_vs_segmented_difference(self):
        cues = load_cues("case_shift_in.srt")  # [1,2)A, [5,6)B
        # Uniform -3s is rejected (A would go negative)...
        with self.assertRaises(sm.OutOfBoundsError):
            sm.shift_cues(cues, -3000)
        # ...while the segmented variant only moves B and succeeds.
        shifted = sm.shift_by_segments(cues, [sm.Segment(4000, 8000, -3000)])
        self.assertEqual((shifted[0].start, shifted[0].end), (1000, 2000))
        self.assertEqual((shifted[1].start, shifted[1].end), (2000, 3000))

    def test_out_of_bounds_reports_every_violation_and_changes_nothing(self):
        cues = load_cues("case_oob_in.srt")
        with self.assertRaises(sm.OutOfBoundsError) as ctx:
            sm.shift_cues(cues, -10_000)
        violations = ctx.exception.violations
        self.assertEqual(len(violations), 1)
        cue, new_start, new_end = violations[0]
        self.assertEqual(cue.text, "LATE")
        self.assertEqual((new_start, new_end), (-9000, -8000))
        # input untouched
        self.assertEqual((cues[0].start, cues[0].end), (1000, 2000))

    def test_no_truncation_at_zero_boundary(self):
        cues = [sm.Cue(500, 1500, "x")]
        with self.assertRaises(sm.OutOfBoundsError):
            sm.shift_cues(cues, -501)
        shifted = sm.shift_cues(cues, -500)  # exactly 0 is legal
        self.assertEqual(shifted[0].start, 0)

    def test_zero_duration_rejected_by_shift(self):
        with self.assertRaises(sm.SubtitleError):
            sm.shift_cues([sm.Cue(1000, 1000, "zero")], 100)

    def test_overlapping_segments_rejected(self):
        with self.assertRaises(sm.SubtitleError):
            sm.shift_by_segments(
                [sm.Cue(0, 100, "x")],
                [sm.Segment(0, 10, 1), sm.Segment(5, 20, 1)],
            )

    def test_segment_boundary_is_half_open(self):
        cues = [sm.Cue(1000, 1500, "edge")]
        shifted = sm.shift_by_segments(cues, [sm.Segment(0, 1000, 500)])
        self.assertEqual(shifted[0].start, 1000)  # start==seg.end -> outside
        shifted = sm.shift_by_segments(cues, [sm.Segment(1000, 2000, 500)])
        self.assertEqual(shifted[0].start, 1500)  # start==seg.start -> inside


class TestMergeProperties(unittest.TestCase):
    def test_output_sorted_and_disjoint(self):
        rng = random.Random(7)
        cues = []
        for _ in range(2000):
            start = rng.randrange(0, 10_000)
            cues.append(sm.Cue(start, start + rng.randrange(1, 500), "t"))
        merged = sm.merge_cues(cues).cues
        for prev, cur in zip(merged, merged[1:]):
            self.assertLessEqual(prev.end, cur.start)  # disjoint, sorted
            self.assertGreater(cur.duration, 0)

    def test_merge_is_idempotent(self):
        once = sm.merge_cues(load_cues("case_chain_a.srt"), load_cues("case_chain_b.srt")).cues
        twice = sm.merge_cues(once).cues
        self.assertEqual(sm.format_srt(once), sm.format_srt(twice))

    def test_text_order_follows_start_time(self):
        merged = sm.merge_cues(
            [sm.Cue(0, 100, "first"), sm.Cue(50, 60, "second")]
        ).cues
        self.assertEqual(merged[0].text, "first\nsecond")


class TestBenchmark(unittest.TestCase):
    def test_merge_50k(self):
        rng = random.Random(42)
        lists = []
        for _ in range(2):
            cues, t = [], 0
            for _ in range(25_000):
                t += rng.randrange(0, 3000)  # heavy overlap on purpose
                cues.append(sm.Cue(t, t + rng.randrange(500, 4000), "lorem ipsum"))
            lists.append(cues)

        tracemalloc.start()
        t0 = time.perf_counter()
        result = sm.merge_cues(*lists)
        elapsed = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        print(
            f"\n[benchmark] merge 50,000 cues -> {len(result.cues)} cues | "
            f"{elapsed * 1000:.1f} ms | peak python heap {peak / 2**20:.1f} MiB"
        )
        for prev, cur in zip(result.cues, result.cues[1:]):
            self.assertLessEqual(prev.end, cur.start)


if __name__ == "__main__":
    unittest.main(verbosity=2)

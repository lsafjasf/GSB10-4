"""Unit tests for keyframe.py, including differential tests vs reference.py.

Run:  python3 -m unittest test_keyframe -v
"""

import random
import unittest

from keyframe import (
    KeyframeTrack,
    OutOfRangeError,
    cubic_bezier,
    ease_in,
    ease_in_out,
    ease_out,
    linear,
    sampled_curve,
)
from reference import ReferenceTrack


class TestLinear(unittest.TestCase):
    def test_midpoint(self):
        tr = KeyframeTrack([(0, 0), (10, 100)])
        self.assertEqual(tr.evaluate(5), 50.0)

    def test_reversed_input_order(self):
        tr = KeyframeTrack([(2, 30), (0, 0), (1, 10)])
        self.assertEqual(tr.evaluate(0.5), 5.0)
        self.assertEqual(tr.evaluate(1.5), 20.0)
        self.assertEqual(tr.evaluate(2), 30.0)

    def test_endpoints_bit_exact(self):
        # 0.1 / 0.3 are not exactly representable: naive v0+(v1-v0)*1 would
        # NOT round-trip; the u-snap must make endpoints bit-exact.
        kfs = [(0.0, 0.1), (0.3, 0.3), (0.7, -1e9), (1.0, 2.5)]
        tr = KeyframeTrack(kfs, easing=ease_in_out)
        for t, v in kfs:
            self.assertEqual(tr.evaluate(t), v, f"t={t}")
            self.assertTrue(type(tr.evaluate(t)) is float)

    def test_endpoints_exact_all_easings(self):
        easings = [linear, ease_in, ease_out, ease_in_out,
                   cubic_bezier(0.42, 0.0, 0.58, 1.0),
                   sampled_curve([(0, 0), (0.3, 0.9), (1, 1)])]
        for e in easings:
            tr = KeyframeTrack([(1.0, 7.0), (2.0, -3.0)], easing=e)
            self.assertEqual(tr.evaluate(1.0), 7.0)
            self.assertEqual(tr.evaluate(2.0), -3.0)


class TestDuplicatesAndZeroDuration(unittest.TestCase):
    KFS = [(0.0, 0.0), (1.0, 10.0), (1.0, 20.0), (2.0, 30.0)]

    def test_last_write_wins_at_exact_time(self):
        tr = KeyframeTrack(self.KFS)
        self.assertEqual(tr.evaluate(1.0), 20.0)

    def test_left_limit_uses_first_duplicate(self):
        tr = KeyframeTrack(self.KFS)
        # Approaching t=1 from the left interpolates toward the FIRST
        # keyframe authored at t=1 (value 10).
        self.assertAlmostEqual(tr.evaluate(1.0 - 1e-9), 10.0, places=3)

    def test_right_segment_uses_last_duplicate(self):
        tr = KeyframeTrack(self.KFS)
        self.assertEqual(tr.evaluate(1.5), 25.0)  # (1,20) -> (2,30)

    def test_all_keyframes_same_time(self):
        # Degenerate zero-duration track: only the last keyframe matters.
        tr = KeyframeTrack([(1.0, 5.0), (1.0, 7.0), (1.0, 9.0)])
        self.assertEqual(tr.evaluate(1.0), 9.0)
        self.assertEqual(tr.evaluate(-100.0), 9.0)   # hold
        self.assertEqual(tr.evaluate(100.0), 9.0)    # hold

    def test_zero_duration_segment_is_skipped(self):
        # A zero-duration segment between duplicates must never be selected.
        tr = KeyframeTrack([(0.0, 0.0), (1.0, 1.0), (1.0, 2.0), (2.0, 4.0)])
        for k in range(21):
            t = k / 20.0
            ref = ReferenceTrack([(0.0, 0.0), (1.0, 1.0), (1.0, 2.0), (2.0, 4.0)])
            self.assertEqual(tr.evaluate(t), ref.evaluate(t))


class TestSingleKeyframe(unittest.TestCase):
    def test_hold(self):
        tr = KeyframeTrack([(3.0, 42.0)], extrapolate="hold")
        self.assertEqual(tr.evaluate(3.0), 42.0)
        self.assertEqual(tr.evaluate(-1.0), 42.0)
        self.assertEqual(tr.evaluate(99.0), 42.0)

    def test_error(self):
        tr = KeyframeTrack([(3.0, 42.0)], extrapolate="error")
        self.assertEqual(tr.evaluate(3.0), 42.0)
        with self.assertRaises(OutOfRangeError):
            tr.evaluate(2.999)
        with self.assertRaises(OutOfRangeError):
            tr.evaluate(3.001)

    def test_slope_is_zero(self):
        tr = KeyframeTrack([(3.0, 42.0)], extrapolate="slope")
        self.assertEqual(tr.evaluate(0.0), 42.0)
        self.assertEqual(tr.evaluate(10.0), 42.0)


class TestExtrapolation(unittest.TestCase):
    KFS = [(0.0, 0.0), (1.0, 2.0), (3.0, 6.0)]

    def test_hold_is_explicit_not_silent(self):
        tr = KeyframeTrack(self.KFS, extrapolate="hold")
        self.assertEqual(tr.evaluate(-5.0), 0.0)
        self.assertEqual(tr.evaluate(5.0), 6.0)

    def test_error_raises_both_sides(self):
        tr = KeyframeTrack(self.KFS, extrapolate="error")
        with self.assertRaises(OutOfRangeError):
            tr.evaluate(-1e-12)
        with self.assertRaises(OutOfRangeError):
            tr.evaluate(3.0 + 1e-12)
        self.assertEqual(tr.evaluate(0.0), 0.0)  # boundary itself is in range
        self.assertEqual(tr.evaluate(3.0), 6.0)

    def test_slope(self):
        tr = KeyframeTrack(self.KFS, extrapolate="slope")
        self.assertEqual(tr.evaluate(-1.0), -2.0)   # slope 2 on [0,1]
        self.assertEqual(tr.evaluate(4.0), 8.0)     # slope 2 on [1,3]
        self.assertEqual(tr.evaluate(4.5), 9.0)

    def test_bad_mode_rejected(self):
        with self.assertRaises(ValueError):
            KeyframeTrack(self.KFS, extrapolate="clamp")


class TestEasings(unittest.TestCase):
    def test_bezier_endpoints_and_monotonic(self):
        e = cubic_bezier(0.42, 0.0, 0.58, 1.0)
        self.assertEqual(e(0.0), 0.0)
        self.assertEqual(e(1.0), 1.0)
        prev = -1.0
        for k in range(101):
            v = e(k / 100.0)
            self.assertGreaterEqual(v, prev)
            prev = v

    def test_bezier_accuracy(self):
        # Known value: cubic-bezier(0.25, 0.1, 0.25, 1) at u=0.5 ~ 0.80240339
        e = cubic_bezier(0.25, 0.1, 0.25, 1.0)
        self.assertAlmostEqual(e(0.5), 0.8024033858, delta=1e-8)

    def test_bezier_rejects_bad_control_points(self):
        with self.assertRaises(ValueError):
            cubic_bezier(-0.1, 0.0, 0.5, 1.0)
        with self.assertRaises(ValueError):
            cubic_bezier(0.5, 0.0, 1.1, 1.0)

    def test_sampled_curve(self):
        e = sampled_curve([(0.0, 0.0), (0.5, 0.8), (1.0, 1.0)])
        self.assertEqual(e(0.0), 0.0)
        self.assertEqual(e(0.5), 0.8)
        self.assertEqual(e(1.0), 1.0)
        self.assertAlmostEqual(e(0.25), 0.4)

    def test_sampled_curve_validation(self):
        with self.assertRaises(ValueError):
            sampled_curve([(0.1, 0.0), (1.0, 1.0)])       # must start at (0,0)
        with self.assertRaises(ValueError):
            sampled_curve([(0.0, 0.0), (0.9, 1.0)])       # must end at (1,1)
        with self.assertRaises(ValueError):
            sampled_curve([(0.0, 0.0)])                   # too few points

    def test_per_segment_easing(self):
        tr = KeyframeTrack([
            (0.0, 0.0, linear),
            (1.0, 1.0, ease_in),
            (2.0, 2.0),
        ])
        self.assertEqual(tr.evaluate(0.5), 0.5)
        self.assertEqual(tr.evaluate(1.5), 1.0 + 0.25)  # ease_in(0.5)=0.25


class TestDifferential(unittest.TestCase):
    """Fast path must agree with the naive reference on every frame."""

    def _make_random_track(self, rng, extrapolate):
        n = rng.randint(1, 40)
        kfs = []
        for _ in range(n):
            # Quantize times to force duplicate / zero-duration cases.
            t = float(rng.randint(0, 20))
            v = rng.uniform(-100, 100)
            e = rng.choice([linear, ease_in, ease_out, ease_in_out,
                            cubic_bezier(0.3, 0.2, 0.7, 0.8)])
            kfs.append((t, v, e))
        rng.shuffle(kfs)  # also exercise unsorted input
        return (KeyframeTrack(kfs, extrapolate=extrapolate),
                ReferenceTrack(kfs, extrapolate=extrapolate))

    def test_random_tracks_match_reference(self):
        rng = random.Random(20261004)
        for trial in range(200):
            extrapolate = rng.choice(["hold", "slope", "error"])
            fast, ref = self._make_random_track(rng, extrapolate)
            for k in range(-5, 106):  # covers outside the timeline too
                t = k / 4.0
                if extrapolate == "error" and not (fast.start <= t <= fast.end):
                    with self.assertRaises(OutOfRangeError):
                        fast.evaluate(t)
                    with self.assertRaises(OutOfRangeError):
                        ref.evaluate(t)
                else:
                    self.assertEqual(
                        fast.evaluate(t), ref.evaluate(t),
                        f"trial={trial} t={t} mode={extrapolate}")


if __name__ == "__main__":
    unittest.main()

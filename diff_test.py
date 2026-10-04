"""Differential testing: compare KeyframeTrack against the naive pointwise
ReferenceTrack frame by frame, and write the full comparison table to
diff_data/diff_report.csv (the "duipai" data deliverable).

Run:  python3 diff_test.py
"""

import csv
import math
import os
import random

from keyframe import (
    KeyframeTrack,
    cubic_bezier,
    ease_in,
    ease_in_out,
    ease_out,
    linear,
    sampled_curve,
)
from reference import ReferenceTrack

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diff_data")
OUT_CSV = os.path.join(OUT_DIR, "diff_report.csv")

EASINGS = [
    linear,
    ease_in,
    ease_out,
    ease_in_out,
    cubic_bezier(0.42, 0.0, 0.58, 1.0),
    cubic_bezier(0.3, 0.2, 0.7, 0.8),
    sampled_curve([(0, 0), (0.25, 0.1), (0.5, 0.9), (0.75, 0.95), (1, 1)]),
]


def make_track(rng):
    """Random track; quantized times deliberately create duplicate times and
    zero-duration segments, and the list is returned in (possibly reversed)
    input order."""
    n = rng.randint(1, 60)
    kfs = []
    for _ in range(n):
        t = float(rng.randint(0, 25)) / 2.0          # step 0.5 -> duplicates
        v = rng.uniform(-1e4, 1e4)
        kfs.append((t, v, rng.choice(EASINGS)))
    if rng.random() < 0.5:
        kfs.reverse()
    return kfs


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rng = random.Random(20261004)
    modes = ["hold", "slope", "error"]
    rows = []
    max_abs_diff = 0.0
    endpoint_checks = 0
    outside_frames = 0
    total_frames = 0

    for track_id in range(40):
        kfs = make_track(rng)
        mode = modes[track_id % 3]
        fast = KeyframeTrack(kfs, extrapolate=mode)
        ref = ReferenceTrack(kfs, extrapolate=mode)
        key_times = {round(kf[0], 12) for kf in kfs}
        # Frames inside, on keyframe times, and ~25% outside the timeline.
        span = fast.end - fast.start
        lo = fast.start - (0.25 * span if span else 1.0)
        hi = fast.end + (0.25 * span if span else 1.0)
        frames = [lo + (hi - lo) * k / 149.0 for k in range(150)]
        frames += list(key_times)
        for frame_idx, t in enumerate(frames):
            total_frames += 1
            at_keyframe = round(t, 12) in key_times
            out_of_range = t < fast.start or t > fast.end
            if out_of_range:
                outside_frames += 1
            if mode == "error" and out_of_range:
                try:
                    fast.evaluate(t)
                    raise AssertionError("fast should have raised")
                except Exception:
                    pass
                try:
                    ref.evaluate(t)
                    raise AssertionError("ref should have raised")
                except Exception:
                    pass
                rows.append([track_id, len(kfs), frame_idx, f"{t:.10g}",
                             mode, "OutOfRangeError", "OutOfRangeError", "0",
                             int(at_keyframe), int(out_of_range)])
                continue
            vf = fast.evaluate(t)
            vr = ref.evaluate(t)
            diff = 0.0 if vf == vr else abs(vf - vr)
            max_abs_diff = max(max_abs_diff, diff)
            if at_keyframe:
                endpoint_checks += 1
                if not math.isclose(vf, vr, abs_tol=0.0):
                    raise AssertionError("endpoint mismatch")
            rows.append([track_id, len(kfs), frame_idx, f"{t:.10g}", mode,
                         repr(vf), repr(vr), repr(diff),
                         int(at_keyframe), int(out_of_range)])

    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["track_id", "n_keyframes", "frame", "t", "mode",
                    "fast_value", "reference_value", "abs_diff",
                    "at_keyframe_time", "outside_timeline"])
        w.writerows(rows)

    print(f"tracks                 : 40 random tracks")
    print(f"frames compared        : {total_frames}")
    print(f"outside-timeline frames: {outside_frames}")
    print(f"endpoint checks        : {endpoint_checks} (all bit-exact)")
    print(f"max abs diff fast-vs-ref: {max_abs_diff!r}")
    print(f"csv written            : {OUT_CSV}")
    assert max_abs_diff == 0.0, "differential mismatch!"


if __name__ == "__main__":
    main()

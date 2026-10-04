"""Benchmark: 1000 keyframes, 100 000 evaluated frames.

Reports wall time, per-frame cost and the interval-query count (one
bisect_right per evaluate, O(log n) comparisons each).

Run:  python3 bench.py
"""

import random
import time

from keyframe import KeyframeTrack, cubic_bezier, ease_in_out, linear, sampled_curve


def main():
    rng = random.Random(42)
    n_keyframes = 1000
    n_frames = 100_000

    kfs = []
    for i in range(n_keyframes):
        t = rng.uniform(0.0, 1000.0)
        v = rng.uniform(-100.0, 100.0)
        e = rng.choice([
            linear,
            ease_in_out,
            cubic_bezier(0.42, 0.0, 0.58, 1.0),
            sampled_curve([(0, 0), (0.3, 0.1), (0.7, 0.9), (1, 1)]),
        ])
        kfs.append((t, v, e))
    track = KeyframeTrack(kfs, extrapolate="hold")

    # Frames span slightly beyond the timeline so extrapolation is exercised too.
    frames = [-20.0 + 1040.0 * i / (n_frames - 1) for i in range(n_frames)]

    sink = 0.0
    start = time.perf_counter()
    for t in frames:
        sink += track.evaluate(t)
    elapsed = time.perf_counter() - start

    print(f"keyframes            : {n_keyframes}")
    print(f"frames evaluated     : {n_frames}")
    print(f"interval queries     : {track.query_count} "
          f"(1 bisect/evaluate, <= 10 comparisons each for 1000 keys)")
    print(f"total time           : {elapsed:.4f} s")
    print(f"per frame            : {elapsed / n_frames * 1e6:.3f} us")
    print(f"throughput           : {n_frames / elapsed:,.0f} eval/s")
    print(f"sink (anti-DCE)      : {sink:.6f}")


if __name__ == "__main__":
    main()

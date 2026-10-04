"""Benchmark: 100k frames x 1000 keyframes. Run: python3 bench.py"""

import random
import time

from keyframe import EASE_IN_OUT, CubicBezier, Keyframe, Timeline, linear


def main():
    rng = random.Random(42)
    n_kf, n_frames = 1000, 100_000
    easings = [linear, EASE_IN_OUT, CubicBezier(0.33, 1.0, 0.68, 1.0)]
    kfs = [
        Keyframe(rng.uniform(0.0, 100.0), rng.uniform(-1.0, 1.0), rng.choice(easings))
        for _ in range(n_kf)
    ]
    frames = [rng.uniform(0.0, 100.0) for _ in range(n_frames)]

    tl = Timeline(kfs)
    # warmup
    tl.evaluate(frames[:1000])
    tl.interval_queries = 0
    tl.bisect_comparisons = 0

    t0 = time.perf_counter()
    values = tl.evaluate(frames)
    dt = time.perf_counter() - t0

    print(f"keyframes           : {n_kf}")
    print(f"frames              : {n_frames}")
    print(f"total time          : {dt*1e3:.1f} ms")
    print(f"per frame           : {dt/n_frames*1e6:.3f} us")
    print(f"interval queries    : {tl.interval_queries}")
    print(f"bisect comparisons  : {tl.bisect_comparisons} "
          f"(avg {tl.bisect_comparisons/tl.interval_queries:.2f}/query, "
          f"log2(1000)~9.97)")
    print(f"checksum            : {sum(values):.6f}")


if __name__ == "__main__":
    main()

"""100 万行基准：帧边界计算耗时 + 内存峰值（仅标准库）。

运行: python3 bench.py
"""

import random
import resource
import time
import tracemalloc

from framecalc import (
    Bound, BoundType, FrameMode, FrameSpec,
    compute_frames, sort_order, window_aggregate,
)

UBP = Bound(BoundType.UNBOUNDED_PRECEDING)
CUR = Bound(BoundType.CURRENT_ROW)


def P(n):
    return Bound(BoundType.PRECEDING, n)


def F(n):
    return Bound(BoundType.FOLLOWING, n)


N = 1_000_000


def rss_mb():
    # Linux: ru_maxrss 单位 KB
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def bench(label, fn):
    start_rss = rss_mb()
    tracemalloc.start()
    t0 = time.perf_counter()
    result = fn()
    dt = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print("%-46s %8.3f s   堆峰值 %+8.1f MB   RSS 峰值 %8.1f MB"
          % (label, dt, peak / 1e6, rss_mb()))
    return result


def main():
    print("行数: %d (单分区, 键含大量并列: 值域 0..999)" % N)
    rng = random.Random(20261006)
    keys = [rng.randint(0, 999) for _ in range(N)]
    vals = [rng.randint(-100, 100) for _ in range(N)]
    print("基线 RSS: %.1f MB（数据载入后）" % rss_mb())

    order = bench("排序 sort_order (key, 原始行号)",
                  lambda: sort_order(keys))
    skeys = [keys[i] for i in order]
    svals = [vals[i] for i in order]

    specs = [
        ("ROWS 1 PRECEDING..1 FOLLOWING",
         FrameSpec(FrameMode.ROWS, P(1), F(1))),
        ("ROWS UNBOUNDED PRECEDING..CURRENT ROW",
         FrameSpec(FrameMode.ROWS, UBP, CUR)),
        ("RANGE 10 PRECEDING..10 FOLLOWING",
         FrameSpec(FrameMode.RANGE, P(10), F(10))),
        ("GROUPS 1 PRECEDING..1 FOLLOWING",
         FrameSpec(FrameMode.GROUPS, P(1), F(1))),
    ]
    for label, spec in specs:
        bench("compute_frames  " + label,
              lambda s=spec: compute_frames(s, skeys))

    for label, spec in specs[:2]:
        bench("window_aggregate(sum)  " + label,
              lambda s=spec: window_aggregate(s, keys, vals, "sum"))

    # 校验和，防止结果被优化掉，同时给出可复现性指纹
    r = window_aggregate(specs[0][1], keys, vals, "sum")
    print("校验指纹 sum-of-sums (ROWS 1..1): %d" % sum(r))


if __name__ == "__main__":
    main()

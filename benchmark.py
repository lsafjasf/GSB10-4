"""5 万条字幕合并基准：耗时 + 内存峰值（tracemalloc）。

运行: python3 benchmark.py
"""

import random
import time
import tracemalloc

from subtitle_tool.core import merge_sources, shift_global, shift_segmented, ShiftWindow


def gen_source(rng, n, lo=0, hi=200_000_000):
    cues = []
    from subtitle_tool.core import Cue
    for _ in range(n):
        start = rng.randrange(lo, hi)
        cues.append(Cue(start, start + rng.randrange(1, 5000), "x"))
    return cues


def main():
    rng = random.Random(20261004)
    a = gen_source(rng, 25_000)
    b = gen_source(rng, 25_000)
    total = len(a) + len(b)

    tracemalloc.start()
    t0 = time.perf_counter()
    result = merge_sources([a, b])
    t1 = time.perf_counter()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"输入: {total} 条 (25000 + 25000)")
    print(f"合并: {t1 - t0:.3f} s, 内存峰值 {peak / 1024 / 1024:.1f} MiB, "
          f"输出 {len(result.cues)} 条, 丢弃报告 {len(result.reports)} 条")

    t0 = time.perf_counter()
    r = shift_global(result.cues, -500, strict=False)
    t1 = time.perf_counter()
    print(f"整段偏移: {t1 - t0:.3f} s, 越界报告 {len(r.reports)} 条")

    windows = [ShiftWindow(i * 10_000_000, i * 10_000_000 + 5_000_000, -1000)
               for i in range(20)]
    t0 = time.perf_counter()
    r = shift_segmented(result.cues, windows, strict=False)
    t1 = time.perf_counter()
    print(f"分段偏移: {t1 - t0:.3f} s, 报告 {len(r.reports)} 条")


if __name__ == "__main__":
    main()

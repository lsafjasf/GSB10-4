"""bench_window_frame.py — 100 万行规模性能基准（仅标准库）。

输出：每种帧的计算耗时、tracemalloc 增量峰值、进程 RSS 峰值，
以及重复运行结果一致性（SHA-256）。
运行：python3 bench_window_frame.py [行数]
"""

import gc
import hashlib
import random
import resource
import sys
import time
import tracemalloc

from window_frame import (
    UNBOUNDED, CURRENT_ROW, PRECEDING, FOLLOWING, Frame,
    evaluate, evaluate_naive,
)

UB, CR, P, F = UNBOUNDED, CURRENT_ROW, PRECEDING, FOLLOWING

FRAMES = [
    ("ROWS  UNBOUNDED PRECEDING..CURRENT ROW",
     Frame("ROWS", (UB, None), (CR, None))),
    ("ROWS  100 PRECEDING..100 FOLLOWING (min)",
     Frame("ROWS", (P, 100), (F, 100)), "min"),
    ("RANGE 1000 PRECEDING..CURRENT ROW (sum)",
     Frame("RANGE", (P, 1000), (CR, None)), "sum"),
    ("RANGE UNBOUNDED PRECEDING..CURRENT ROW (默认帧, sum)",
     Frame("RANGE", (UB, None), (CR, None)), "sum"),
    ("GROUPS 5 PRECEDING..5 FOLLOWING (sum)",
     Frame("GROUPS", (P, 5), (F, 5)), "sum"),
    ("ROWS  UNBOUNDED PRECEDING..UNBOUNDED FOLLOWING (全帧, avg)",
     Frame("ROWS", (UB, None), (UB, None)), "avg"),
]


def digest(seq):
    h = hashlib.sha256()
    for x in seq:
        h.update(repr(x).encode())
        h.update(b",")
    return h.hexdigest()[:16]


def rss_mb():
    # Linux: ru_maxrss 单位 KB；macOS: 字节。这里是 Linux 环境。
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    print("行数 n = %d，Python %s" % (n, sys.version.split()[0]))

    rng = random.Random(20261006)
    t0 = time.perf_counter()
    rows = [(rng.randrange(100), rng.randrange(10_000), rng.randrange(100_000))
            for _ in range(n)]
    build_sec = time.perf_counter() - t0
    print("造数耗时 %.2f s，输入构造后进程 RSS 峰值 %.1f MB" % (build_sec, rss_mb()))

    kw = dict(partition_key=lambda r: r[0],
              order_key=lambda r: r[1],
              value=lambda r: r[2])

    # ---- 正确性：1 万行抽样，快速实现 vs 朴素实现逐元素对拍 ----
    print("\n[正确性] 前 10000 行抽样对拍：")
    sample = rows[:10_000]
    for spec in FRAMES:
        name, frame = spec[0], spec[1]
        agg = spec[2] if len(spec) > 2 else "sum"
        got = evaluate(sample, frame, agg, **kw)
        ref = evaluate_naive(sample, frame, agg, **kw)
        assert len(got) == len(ref) == len(sample)
        ok = True
        for a, b in zip(got, ref):
            if isinstance(a, float) or isinstance(b, float):
                if abs((a or 0) - (b or 0)) > 1e-9:
                    ok = False
                    break
            elif a != b:
                ok = False
                break
        print("  %-52s %s" % (name, "对拍通过" if ok else "不一致!"))
        assert ok

    # ---- 性能：耗时（3 次取最优）+ tracemalloc 增量峰值 ----
    print("\n[性能] n=%d（耗时为 3 次最优值）" % n)
    print("  %-52s %8s %12s %10s" % ("帧 / 聚合", "耗时(s)", "增量峰值(MB)", "吞吐(万行/s)"))
    hashes = []
    for spec in FRAMES:
        name, frame = spec[0], spec[1]
        agg = spec[2] if len(spec) > 2 else "sum"
        best = float("inf")
        peak = 0
        last_out = None
        for run in range(3):
            gc.collect()
            tracemalloc.start()
            t0 = time.perf_counter()
            out = evaluate(rows, frame, agg, **kw)
            dt = time.perf_counter() - t0
            _, p = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            best = min(best, dt)
            peak = max(peak, p / 1024.0 / 1024.0)
            last_out = out
        hashes.append((name, digest(last_out)))
        print("  %-52s %8.2f %12.1f %10.1f"
              % (name, best, peak, n / best / 10_000))

    print("\n[内存] 全部跑完后进程 RSS 高水位：%.1f MB" % rss_mb())

    # ---- 可复现性：对每个帧再独立跑两次，哈希必须一致 ----
    print("\n[可复现] 同输入重复运行结果 SHA-256（前 16 位）：")
    for spec in FRAMES:
        name, frame = spec[0], spec[1]
        agg = spec[2] if len(spec) > 2 else "sum"
        hs = []
        for run in range(3):
            out = evaluate(rows, frame, agg, **kw)
            hs.append(digest(out))
        same = len(set(hs)) == 1
        print("  %-52s %s  %s"
              % (name, hs[0], "3 次一致" if same else "不一致! %r" % hs))
        assert same


if __name__ == "__main__":
    main()

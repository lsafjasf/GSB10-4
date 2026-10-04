"""100 万区间合并基准：耗时 + 内存峰值。

运行：
  python3 benchmark.py              # 默认 1,000,000 条
  python3 benchmark.py --n 1000000 --touch both
"""

import argparse
import json
import os
import platform
import random
import resource
import time
import tracemalloc

from imerge import merge_intervals


def gen_pairs(n, seed=20261005):
    rng = random.Random(seed)
    span_us = 100 * 365 * 86_400_000_000  # 100 年，微秒时间轴
    pairs = []
    for _ in range(n):
        s = rng.randrange(0, span_us)
        if rng.random() < 0.05:
            pairs.append((s, s))  # 5% 零长度点
        else:
            length = rng.randrange(1, 600_000_000)  # 1 微秒 ~ 10 分钟
            pairs.append((s, s + length))
    return pairs


def bench_once(pairs, merge_touching):
    tracemalloc.start()
    t0 = time.perf_counter()
    result = merge_intervals(pairs, merge_touching=merge_touching)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, elapsed, peak


def bench_modes(pairs, merge_touching, repeat=3):
    """重复多次，报告最好/平均耗时与首次 tracemalloc 峰值。"""
    times = []
    peak = 0
    result = []
    for r in range(repeat):
        result, elapsed, peak_run = bench_once(pairs, merge_touching)
        times.append(elapsed)
        peak = max(peak, peak_run)
    return result, min(times), sum(times) / len(times), peak


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1_000_000)
    ap.add_argument("--touch", choices=["false", "true", "both"], default="both")
    args = ap.parse_args()

    print("生成 %d 条区间 ..." % args.n)
    pairs = gen_pairs(args.n)
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    report = {
        "n": args.n,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "runs": [],
    }
    modes = {"false": False, "true": True}
    for name in (["false", "true"] if args.touch == "both" else [args.touch]):
        result, best, avg, peak = bench_modes(pairs, modes[name])
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        run = {
            "merge_touching": modes[name],
            "output_count": len(result),
            "best_elapsed_seconds": round(best, 3),
            "avg_elapsed_seconds": round(avg, 3),
            "tracemalloc_peak_mb": round(peak / 1024 / 1024, 1),
            "process_maxrss_mb": round(rss_after / 1024, 1),
        }
        report["runs"].append(run)
        print(json.dumps(run, ensure_ascii=False))

    report["process_maxrss_note"] = (
        "ru_maxrss 为进程级峰值（含解释器与输入数据），基准前为 %.1f MB" % (rss_before / 1024,)
    )
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "benchmark.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("写出 %s" % out)


if __name__ == "__main__":
    main()

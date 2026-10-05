# -*- coding: utf-8 -*-
"""
bench.py —— 1000 万行 / 64MB 内存上限基准

用法：
    python3 bench.py --gen                 # 生成 1000 万行数据文件（约 190MB）
    python3 bench.py --run                 # 预算 32MB 跑聚合，报告耗时与峰值内存
    python3 bench.py --run --budget 8      # 换 8MB 预算（不同溢出形态）做对拍
    python3 bench.py --check               # 对比两次 --run 的结果摘要是否一致

内存口径：哈希表估算预算 --budget MB；进程峰值 RSS 由 getrusage 给出，
要求 < --limit MB（默认 64）。
"""
import argparse
import json
import os
import random
import resource
import sys
import time

from hashagg import SpillAggregator

DATA = "bench_data.txt"
OUT = "bench_result.json"
OPS = [("count", None), ("sum", 1), ("min", 2), ("max", 2)]


def gen(path=DATA, rows=10_000_000, n_keys=6_000_000, hot_share=0.25, seed=7):
    rng = random.Random(seed)
    t0 = time.time()
    with open(path, "w", buffering=1 << 20) as f:
        for _ in range(rows):
            if rng.random() < hot_share:
                k = 0
            else:
                k = 1 + rng.randrange(n_keys)
            f.write("%d %d %d\n" % (k, rng.randint(-10**6, 10**6),
                                    rng.randint(0, 10**6)))
    mb = os.path.getsize(path) / 1e6
    print("生成 %s: %d 行, %.0f MB, %.1fs" % (path, rows, mb, time.time() - t0))


def iter_rows(path=DATA):
    with open(path, "r", buffering=1 << 20) as f:
        for line in f:
            k, a, b = line.split()
            yield (int(k), int(a), int(b))


def run(budget_mb, tag):
    budget = budget_mb * 1024 * 1024
    t0 = time.time()
    n_out = 0
    tot_count = 0
    tot_sum = 0
    checksum = 0
    # 输入为文件时按字节数给总量估计（经验：表字节 ~ 5x 文本字节），
    # 让根分区一次分够，减少递归层数与落盘 IO
    est_hint = int(os.path.getsize(DATA) * 5)
    with SpillAggregator(OPS, budget=budget) as agg:
        for key, (c, s, mi, ma) in agg.aggregate(iter_rows(),
                                                 est_total_bytes=est_hint):
            n_out += 1
            tot_count += c
            tot_sum += s
            checksum = (checksum + (hash(key) & 0xFFFFFFFF) * (c + s)) & 0xFFFFFFFFFFFFFFFF
        stats = dict(agg.stats)
    dt = time.time() - t0
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    by_level = {}
    for e in stats["spills"]:
        by_level.setdefault(e["depth"], e["fanout"])
    dist = None
    if stats["spills"]:
        rows_per = sorted(stats["spills"][0]["rows_per_partition"], reverse=True)
        bytes_per = sorted(stats["spills"][0]["bytes_per_partition"], reverse=True)
        mid = len(rows_per) // 2
        dist = {
            "partitions": len(rows_per),
            "rows_max": rows_per[0], "rows_p50": rows_per[mid],
            "rows_min": rows_per[-1],
            "rows_max_over_p50": round(rows_per[0] / max(1, rows_per[mid]), 2),
            "mb_max": round(bytes_per[0] / 1e6, 1),
            "mb_p50": round(bytes_per[mid] / 1e6, 1),
            "mb_min": round(bytes_per[-1] / 1e6, 1),
        }
    summary = {
        "tag": tag,
        "budget_mb": budget_mb,
        "groups": n_out,
        "total_count": tot_count,
        "total_sum": tot_sum,
        "checksum": checksum,
        "wall_s": round(dt, 2),
        "peak_rss_mb": round(peak_mb, 1),
        "spill_events": len(stats["spills"]),
        "max_depth": stats["max_depth"],
        "leaf_partitions": stats["leaf_partitions"],
        "fanout_by_level": by_level,
        "spill_mb": round(stats["spill_bytes"] / 1e6, 1),
        "first_spill_partition_dist": dist,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    out = OUT if tag == "main" else "bench_result_%s.json" % tag
    with open(out, "w") as f:
        json.dump(summary, f)
    return summary


def check():
    a = json.load(open(OUT))
    b = json.load(open("bench_result_small.json"))
    keys = ("groups", "total_count", "total_sum", "checksum")
    same = all(a[k] == b[k] for k in keys)
    print("对拍: 预算 %dMB vs %dMB -> %s"
          % (a["budget_mb"], b["budget_mb"], "结果完全一致" if same else "不一致!"))
    for k in keys:
        print("  %-12s main=%s small=%s" % (k, a[k], b[k]))
    sys.exit(0 if same else 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--budget", type=int, default=24, help="哈希表预算 MB")
    ap.add_argument("--limit", type=int, default=64, help="进程峰值内存上限 MB")
    args = ap.parse_args()
    if args.gen:
        gen()
    if args.run:
        tag = "main" if args.budget == 24 else "small"
        s = run(args.budget, tag)
        if s["peak_rss_mb"] > args.limit:
            print("!! 峰值内存 %.1fMB 超过上限 %dMB" % (s["peak_rss_mb"], args.limit))
            sys.exit(1)
        print("峰值内存 %.1fMB <= 上限 %dMB  OK" % (s["peak_rss_mb"], args.limit))
    if args.check:
        check()


if __name__ == "__main__":
    main()

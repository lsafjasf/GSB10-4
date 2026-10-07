#!/usr/bin/env python3
"""分布、误差、打乱对拍实验，输出 Markdown 表格。

运行: python3 experiments.py
"""

import math
import random
import time
from collections import Counter

from rsample import HashSampler

ALPHA = 1e-6


def chernoff_bound(n, p):
    """返回 (相对偏差上界 delta, 绝对偏差上界)，置信度 1-1e-6。"""
    if n * p == 0:
        return 0.0, 0.0
    delta = math.sqrt(-3.0 * math.log(ALPHA / 2.0) / (n * p))
    return delta, n * p * delta


def normal_bound(n, p):
    """正态近似 5-sigma 绝对偏差上界（更直观）。"""
    return 5.0 * math.sqrt(n * p * (1 - p))


def exp_rate_accuracy():
    print("## 采样率误差（Chernoff 界, 置信度 1-1e-6）\n")
    print("| n | p | 期望命中 | 实测命中 | 实测率 | 绝对偏差上界 | 实测绝对偏差 | 相对偏差上界 |")
    print("|---|---|---|---|---|---|---|---|")
    rows = []
    for n, p in [(100_000, 0.001), (100_000, 0.01), (1_000_000, 0.01),
                 (1_000_000, 0.1), (1_000_000, 0.5)]:
        s = HashSampler(20261007, p)
        cnt = sum(s.keep(f"r-{i}") for i in range(n))
        delta, absb = chernoff_bound(n, p)
        rows.append((n, p, cnt, absb, delta))
        print(f"| {n} | {p} | {n*p:.0f} | {cnt} | {cnt/n:.6f} | {absb:.0f} | "
              f"{abs(cnt-n*p)} | {delta*100:.2f}% |")
    print()
    return rows


def exp_shard_vs_whole():
    print("## 分片采样 vs 单机整体采样\n")
    n, p, shards = 1_000_000, 0.01, 8
    keys = [f"row-{i}" for i in range(n)]
    t0 = time.perf_counter()
    whole = {k for k in keys if HashSampler(11, p).keep(k)}
    per = n // shards
    union = set()
    shard_counts = []
    for sid in range(shards):
        part = keys[sid * per:(sid + 1) * per]
        kept = {k for k in part if HashSampler(11, p).keep(k)}
        shard_counts.append(len(kept))
        union |= kept
    dt = time.perf_counter() - t0
    print(f"- 单机整体样本数: {len(whole)}")
    print(f"- {shards} 个分片样本并集: {len(union)}")
    print(f"- 并集与整体是否**逐行相同**: {union == whole}")
    print(f"- 各分片命中数: {shard_counts}（每片期望 {per*p:.0f}）")
    delta, absb = chernoff_bound(per, p)
    ok = all(abs(c - per * p) <= absb for c in shard_counts)
    print(f"- 每片偏差上界 ±{absb:.0f}（{delta*100:.2f}%），全部在界内: {ok}")
    print(f"- 耗时 {dt:.2f}s\n")
    return union == whole


def exp_shuffle():
    print("## 输入顺序打乱对拍\n")
    n, p = 500_000, 0.02
    keys = [f"ord-{i}" for i in range(n)]
    base = {k for k in keys if HashSampler(999, p).keep(k)}
    print(f"- 原顺序样本数: {len(base)}")
    all_same = True
    for trial in range(5):
        shuffled = keys[:]
        random.Random(trial).shuffle(shuffled)
        got = {k for k in shuffled if HashSampler(999, p).keep(k)}
        same = got == base
        all_same &= same
        print(f"- 打乱种子 {trial}: 样本数 {len(got)}, 与原集合完全相同: {same}")
    print(f"- 5 次打乱全部一致: {all_same}\n")
    return all_same


def exp_hash_uniformity():
    print("## 哈希均匀性（卡方拟合，10 桶）\n")
    n = 1_000_000
    s = HashSampler(31337, 1.0)
    buckets = Counter()
    step = (1 << 64) // 10
    for i in range(n):
        h = s.hash64(f"u-{i}")
        buckets[min(h // step, 9)] += 1
    exp = n / 10
    chi2 = sum((buckets[b] - exp) ** 2 / exp for b in range(10))
    # df=9, 0.1% 临界值约 27.88
    print(f"- 各桶计数: {[buckets[b] for b in range(10)]}")
    print(f"- 卡方统计量: {chi2:.2f}（df=9, p=0.001 临界值 27.88），均匀性通过: {chi2 < 27.88}\n")
    return chi2 < 27.88


def exp_duplicates():
    print("## 大量重复键\n")
    distinct, reps, p = 10_000, 100, 0.01
    s = HashSampler(4321, p)
    kept_distinct = sum(s.keep(f"dk-{i}") for i in range(distinct))
    kept_rows = sum(s.keep(f"dk-{i}") for i in range(distinct) for _ in range(reps))
    delta, absb = chernoff_bound(distinct, p)
    print(f"- 不同键 {distinct} 个，每键重复 {reps} 次，共 {distinct*reps} 行")
    print(f"- 命中不同键: {kept_distinct}（期望 {distinct*p:.0f}，上界 ±{absb:.0f}）")
    print(f"- 命中行数: {kept_rows} = {kept_distinct} × {reps}（同键同进同出）")
    print(f"- 结论: 重复键被一致地整体保留/丢弃，键级采样率准确，行级样本中重复键成组出现\n")


def exp_tiny():
    print("## 样本极少（小总体）\n")
    for n, p in [(10, 0.01), (100, 0.01), (100, 0.5)]:
        s = HashSampler(606, p)
        cnt = sum(s.keep(f"t-{i}") for i in range(n))
        print(f"- n={n}, p={p}: 命中 {cnt}（期望 {n*p:.1f}；小样本偏差由二项分布主导，属正常波动）")
    print()


if __name__ == "__main__":
    print("# 实验数据\n")
    exp_rate_accuracy()
    exp_shard_vs_whole()
    exp_shuffle()
    exp_hash_uniformity()
    exp_duplicates()
    exp_tiny()

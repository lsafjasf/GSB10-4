#!/usr/bin/env python3
"""1000 万行、采样率 1% 的耗时与内存峰值基准。

运行: python3 bench.py
"""

import resource
import sys
import time

from rsample import HashSampler

N = 10_000_000
RATE = 0.01
SEED = 20261007


def row_gen(n):
    for i in range(n):
        yield f"row-{i}"


def peak_rss_mb():
    # Linux: ru_maxrss 单位 KB
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main():
    base_rss = peak_rss_mb()
    sampler = HashSampler(SEED, RATE)

    # 场景 A：纯流式过滤，只统计命中数（采样结果不落内存）
    t0 = time.perf_counter()
    kept = 0
    for key in row_gen(N):
        if sampler.keep(key):
            kept += 1
    t1 = time.perf_counter()
    rss_a = peak_rss_mb()

    # 场景 B：流式过滤并把样本收集到列表（模拟要落盘/返回样本）
    sample = []
    t2 = time.perf_counter()
    for key in row_gen(N):
        if sampler.keep(key):
            sample.append(key)
    t3 = time.perf_counter()
    rss_b = peak_rss_mb()

    assert kept == len(sample)
    print(f"行数            : {N:,}")
    print(f"采样率          : {RATE} (期望命中 {int(N*RATE):,})")
    print(f"实际命中        : {kept:,} (实测率 {kept/N:.6f})")
    print(f"场景A 流式计数  : {t1-t0:.2f}s, 吞吐 {N/(t1-t0)/1e6:.2f}M 行/s")
    print(f"场景B 流式+收集 : {t3-t2:.2f}s, 吞吐 {N/(t3-t2)/1e6:.2f}M 行/s")
    print(f"起始 RSS        : {base_rss:.1f} MB")
    print(f"场景A 峰值 RSS  : {rss_a:.1f} MB (增量 {rss_a-base_rss:.1f} MB)")
    print(f"场景B 峰值 RSS  : {rss_b:.1f} MB (增量 {rss_b-base_rss:.1f} MB, 含 {kept:,} 条样本)")


if __name__ == "__main__":
    main()

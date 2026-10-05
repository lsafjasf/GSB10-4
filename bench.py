"""基准：100 万行 x 10 级排序键，输出耗时与内存峰值。"""
import gc
import random
import resource
import sys
import time
import tracemalloc

from stable_sort import stable_sort

N = 1_000_000
KEYS = [f"k{i}" for i in range(10)]
SPECS = [KEYS[0]] + [(k, i % 2 == 1) for i, k in enumerate(KEYS[1:], 1)]


def gen(n, seed=1):
    rng = random.Random(seed)
    words = ["alpha", "beta", "gamma", "delta", "eps"]
    rows = []
    for _ in range(n):
        row = {}
        for i, k in enumerate(KEYS):
            r = rng.random()
            if r < 0.05:
                row[k] = None                      # 5% 缺失
            elif i % 3 == 0:
                row[k] = rng.randrange(1000)       # 大量平局
            elif i % 3 == 1:
                row[k] = rng.choice(words)
            else:
                row[k] = round(rng.random() * 100, 3)
        rows.append(row)
    return rows


def main():
    rows = gen(N)
    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()
    out = stable_sort(rows, SPECS)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # 抽查稳定性：再排一次，逐元素一致
    t1 = time.perf_counter()
    again = stable_sort(rows, SPECS)
    elapsed2 = time.perf_counter() - t1
    assert [id(r) for r in out] == [id(r) for r in again], "重复排序不一致!"

    print(f"行数            : {N:,}")
    print(f"排序键级数      : {len(SPECS)} (升/降交替: {SPECS})")
    print(f"首次排序耗时    : {elapsed:.2f} s")
    print(f"重复排序耗时    : {elapsed2:.2f} s (结果逐元素一致)")
    print(f"tracemalloc 峰值: {peak / 2**20:.0f} MiB (排序期间 Python 堆增量)")
    print(f"进程 RSS 峰值   : {rss_kb / 1024:.0f} MiB (含数据生成)")
    print(f"Python          : {sys.version.split()[0]}")


if __name__ == "__main__":
    main()

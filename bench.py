"""100 万区间合并基准：耗时 + 内存峰值，结果写入 data/benchmark.txt。"""

import random
import resource
import time
import tracemalloc

from interval_merge import Interval, merge_sorted

N = 1_000_000

rng = random.Random(42)
t0 = time.perf_counter()
intervals = []
for _ in range(N):
    a = rng.randint(0, 1_000_000_000)
    intervals.append(Interval(a, a + rng.randint(0, 100)))
t_gen = time.perf_counter() - t0

tracemalloc.start()
t0 = time.perf_counter()
merged = merge_sorted(intervals)
t_merge = time.perf_counter() - t0
_, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()

maxrss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

lines = [
    f"区间数量: {N:,}",
    f"生成耗时: {t_gen:.3f} s",
    f"合并耗时: {t_merge:.3f} s",
    f"合并结果段数: {len(merged):,}",
    f"tracemalloc 峰值(合并阶段): {peak / 1024 / 1024:.1f} MiB",
    f"进程峰值 RSS(整体): {maxrss_kb / 1024:.1f} MiB",
    f"Python: {__import__('sys').version.split()[0]}",
]
text = "\n".join(lines)
print(text)
with open("data/benchmark.txt", "w") as f:
    f.write(text + "\n")

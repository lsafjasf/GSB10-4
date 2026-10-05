"""压测：10 万顶点（左右各 5 万）、50 万条随机边。

用法: python3 bench.py [seed]
输出: 求解各阶段耗时、tracemalloc 内存峰值、进程 RSS 峰值，以及完整核验报告。
"""

import random
import resource
import sys
import time
import tracemalloc

from bvcover import LEFT, RIGHT, format_report, min_vertex_cover


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    n_left = n_right = 50_000
    m = 500_000
    n = n_left + n_right
    side = [LEFT] * n_left + [RIGHT] * n_right

    rng = random.Random(seed)
    t0 = time.perf_counter()
    edges = [(rng.randrange(n_left), n_left + rng.randrange(n_right))
             for _ in range(m)]
    t1 = time.perf_counter()

    tracemalloc.start()
    t2 = time.perf_counter()
    res = min_vertex_cover(n, side, edges)
    t3 = time.perf_counter()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

    print("规模: %d 顶点（左 %d / 右 %d），%d 条输入边，seed=%d"
          % (n, n_left, n_right, m, seed))
    print("数据生成耗时: %.3f s" % (t1 - t0))
    print("求解总耗时（建图+匹配+König构造+逐边核验）: %.3f s" % (t3 - t2))
    print("tracemalloc 内存峰值: %.1f MiB" % (peak / 1024 / 1024))
    print("进程 RSS 峰值（ru_maxrss）: %.1f MiB" % rss_mb)
    print("-" * 60)
    print(format_report(res))
    assert res.verified and res.optimal


if __name__ == "__main__":
    main()

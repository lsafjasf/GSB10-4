"""基准：10 万点（左右各 5 万）、50 万条随机边（含自然产生的多重边）。

运行: python3 benchmark.py
"""

import random
import resource
import time

from bipartite_cover import BipartiteGraph

N_LEFT = 50_000
N_RIGHT = 50_000
M = 500_000
SEED = 20261005


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main():
    rng = random.Random(SEED)

    t0 = time.perf_counter()
    edges = [(rng.randrange(N_LEFT), rng.randrange(N_RIGHT)) for _ in range(M)]
    t1 = time.perf_counter()

    g = BipartiteGraph((("L", i) for i in range(N_LEFT)),
                       (("R", j) for j in range(N_RIGHT)))
    for u, v in edges:
        g.add_edge(("L", u), ("R", v))
    t2 = time.perf_counter()

    res = g.min_vertex_cover()
    t3 = time.perf_counter()

    # 逐边核验
    uncovered = res.uncovered_edges()
    t4 = time.perf_counter()

    print(f"规模: |L|={N_LEFT}, |R|={N_RIGHT}, 生成边数={M}")
    print(f"唯一边数: {len(g.edges)}, 多重边(重复)数: {len(g.multi_edges)}, 异常边数: {len(g.anomalies)}")
    print(f"最大匹配 |M| = {res.matching_size}")
    print(f"最小点覆盖 |C| = {res.cover_size}  (左部 {len(res.cover_left)} + 右部 {len(res.cover_right)})")
    print(f"下界对照: |C| == |M| -> {res.cover_size == res.matching_size}")
    print(f"逐边核验: 未覆盖边数 = {len(uncovered)}")
    print("--- 耗时 ---")
    print(f"生成边表:        {t1 - t0:8.3f} s")
    print(f"建图(含去重):    {t2 - t1:8.3f} s")
    print(f"匹配+König覆盖:  {t3 - t2:8.3f} s")
    print(f"逐边核验:        {t4 - t3:8.3f} s")
    print(f"求解合计(建图起): {t3 - t1:8.3f} s")
    print(f"内存峰值 (ru_maxrss): {peak_rss_mb():.1f} MiB")


if __name__ == "__main__":
    main()

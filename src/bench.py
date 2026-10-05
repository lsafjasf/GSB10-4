"""基准：10 万点 / 50 万边，报告耗时与内存峰值。

运行：python3 src/bench.py
"""

import os
import random
import resource
import sys
import time
import tracemalloc
from heapq import heappush, heappop

sys.path.insert(0, os.path.dirname(__file__))

from negsp import shortest_paths, INF, NEG_INF                        # noqa: E402

N = 100_000
M = 500_000


def gen_all_positive(seed):
    rng = random.Random(seed)
    edges = [(rng.randrange(N), rng.randrange(N), rng.randint(1, 10))
             for _ in range(M)]
    return edges


def gen_dag_negative(seed):
    """DAG（仅小编号 -> 大编号），含负权边但绝无环。"""
    rng = random.Random(seed)
    edges = []
    for _ in range(M):
        a = rng.randrange(N - 1)
        b = rng.randrange(a + 1, N)
        edges.append((a, b, rng.randint(-5, 10)))
    return edges


def run_case(name, edges):
    src = 0
    tracemalloc.start()
    t0 = time.perf_counter()
    res = shortest_paths(N, edges, src)
    t1 = time.perf_counter()
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    n_inf = sum(1 for d in res.dist if d is INF)
    n_neginf = sum(1 for d in res.dist if d == NEG_INF)
    print(f"[{name}]")
    print(f"  求解耗时      : {t1 - t0:.2f} s")
    print(f"  内存峰值(py)  : {peak / 2**20:.1f} MiB (tracemalloc, 求解阶段)")
    print(f"  内存峰值(进程): {rss_kb / 1024:.1f} MiB (ru_maxrss, 含图数据)")
    print(f"  负环          : {len(res.negative_cycles)} 个")
    print(f"  不可达点      : {n_inf}，-inf 点: {n_neginf}")
    print(f"  抽样距离      : {[res.dist[i] for i in (0, 1, 2, N // 2, N - 1)]}")
    return res


def dijkstra_check(edges, dist):
    """非负权图：用堆优化 Dijkstra 独立复算，逐点核对 Bellman-Ford 结果。"""
    adj = [[] for _ in range(N)]
    for u, v, w in edges:
        adj[u].append((v, w))
    d = [INF] * N
    d[0] = 0
    pq = [(0, 0)]
    while pq:
        du, u = heappop(pq)
        if du > d[u]:
            continue
        for v, w in adj[u]:
            nd = du + w
            if nd < d[v]:
                d[v] = nd
                heappush(pq, (nd, v))
    bad = [v for v in range(N) if d[v] != dist[v]]
    assert not bad, f"Dijkstra 对拍不一致，{len(bad)} 个点，首例 {bad[:5]}"
    print("  Dijkstra 交叉验证: 100000 个点逐点一致")


def main():
    print(f"n={N}  m={M}  source=0")

    t0 = time.perf_counter()
    edges_pos = gen_all_positive(1)
    print(f"图生成(全正权): {time.perf_counter() - t0:.2f} s")
    res = run_case("全正权", edges_pos)
    t0 = time.perf_counter()
    dijkstra_check(edges_pos, res.dist)
    print(f"  Dijkstra 耗时 : {time.perf_counter() - t0:.2f} s")
    del edges_pos

    t0 = time.perf_counter()
    edges_dag = gen_dag_negative(2)
    print(f"图生成(DAG负边): {time.perf_counter() - t0:.2f} s")
    run_case("含负权边无环(DAG)", edges_dag)
    del edges_dag


if __name__ == "__main__":
    main()

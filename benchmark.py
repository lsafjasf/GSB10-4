"""性能基准：n=100000, m=500000。
  场景 A 无负环（含负权边）：单源最短路，与 SPFA 对拍抽点
  场景 B 可达负环：检测 + 环定位
  场景 C 不可达负环：源点视角正常收敛，超级源点检测定位
运行：python3 benchmark.py
"""

import random
import resource
import time
import tracemalloc

from shortest_path import (
    INF,
    bellman_ford,
    cycle_weight,
    find_any_negative_cycle,
    spfa_reference,
)

N = 100_000
M = 500_000


def rss_mb():
    # Linux 上 ru_maxrss 单位为 KB
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def gen_no_neg_cycle(n, m, rng):
    """势函数 reweighting：w = base(>=0) + h[u]-h[v]，保证无负环且含负权边。"""
    h = [rng.randint(-500, 500) for _ in range(n)]
    edges = []
    for _ in range(m):
        u = rng.randrange(n)
        v = rng.randrange(n)
        edges.append((u, v, rng.randint(0, 100) + h[u] - h[v]))
    return edges, sum(1 for _, _, w in edges if w < 0)


def timed(label, fn):
    t0 = time.perf_counter()
    tracemalloc.start()
    result = fn()
    _, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    dt = time.perf_counter() - t0
    print(f"{label}: 耗时 {dt:.3f}s, 本次调用 Python 堆峰值 {py_peak / 1e6:.1f} MB")
    return result, dt, py_peak / 1e6


def main():
    rng = random.Random(42)
    import platform, sys
    print(f"规模: n={N:,}, m={M:,}")
    print(f"环境: {platform.python_implementation()} {sys.version.split()[0]}, "
          f"{platform.platform()}")
    t0 = time.perf_counter()
    edges, neg_edges = gen_no_neg_cycle(N, M, rng)
    print(f"建图: {time.perf_counter()-t0:.3f}s, 其中负权边 {neg_edges:,} 条")
    print(f"建图后进程 RSS 峰值: {rss_mb():.1f} MB\n")

    # ---- 场景 A：无负环（含负权边）----
    print("== 场景 A：无负环（含负权边），单源最短路径 ==")
    (res, tA, pyA) = timed("bellman_ford", lambda: bellman_ford(N, edges, 0))
    print(f"  松弛轮数 {res.rounds}, 可达点 {sum(d != INF for d in res.dist):,}")
    assert not res.has_neg_cycle

    (ref, tRef, _) = timed("spfa 参考实现(抽点对拍)", lambda: spfa_reference(N, edges, 0))
    check = list(range(N))
    bad = sum(res.dist[i] != ref[i] for i in check)
    print(f"  全部 {N:,} 个点逐点对拍，不一致 {bad} 处")
    sample = rng.sample(range(N), 5)
    for i in sample:
        print(f"    点 {i}: bellman_ford={res.dist[i]}  spfa={ref[i]}")
    assert bad == 0

    # ---- 场景 B：可达负环 ----
    print("\n== 场景 B：源点可达的负环（0->1->2->0, 1+1-5=-3）==")
    edges_b = edges + [(0, 1, 1), (1, 2, 1), (2, 0, -5)]
    (resB, tB, pyB) = timed("bellman_ford", lambda: bellman_ford(N, edges_b, 0))
    assert resB.has_neg_cycle and resB.cycle_reachable
    print(f"  第 {resB.rounds} 轮检出负环，环节点 {resB.cycle}, "
          f"环权 {cycle_weight(resB.cycle, edges_b)}")

    # ---- 场景 C：不可达负环 ----
    print("\n== 场景 C：负环在源点不可达的独立分量 {n-3,n-2,n-1} ==")
    a, b, c = N - 3, N - 2, N - 1
    # 底图只生成在前 N-3 个点上（仍用势函数保证无负环），
    # 使 {a,b,c} 成为源点不可达的独立分量
    edges_c_base, _ = gen_no_neg_cycle(a, M, rng)
    edges_c = edges_c_base + [(a, b, 2), (b, c, 2), (c, a, -9)]
    (resC, tC, pyC) = timed("bellman_ford(源点视角)",
                            lambda: bellman_ford(N, edges_c, 0))
    assert not resC.has_neg_cycle
    print(f"  源点视角: 无可达负环，松弛 {resC.rounds} 轮；"
          f"环点距离 {resC.dist[a]}/{resC.dist[b]}/{resC.dist[c]} (INF 即不可达)")
    (cyc, tD, pyD) = None, None, None

    def find_any():
        c2, rounds = find_any_negative_cycle(N, edges_c, return_rounds=True)
        return c2, rounds

    (out, tD, pyD) = timed("find_any_negative_cycle(超级源点)", find_any)
    cyc, rounds = out
    print(f"  第 {rounds} 轮检出全局负环，环节点 {cyc}, 环权 {cycle_weight(cyc, edges_c)}")
    assert set(cyc) == {a, b, c}

    print("\n== 汇总 ==")
    print(f"{'场景':<28}{'耗时(s)':>10}{'调用堆峰值(MB)':>16}")
    for name, t, py in [
        ("A 无负环 bellman_ford", tA, pyA),
        ("A SPFA 参考实现", tRef, None),
        ("B 可达负环检测", tB, pyB),
        ("C 源点视角 bellman_ford", tC, pyC),
        ("C 超级源点负环检测", tD, pyD),
    ]:
        print(f"{name:<28}{t:>10.3f}{('%.1f' % py) if py else '-':>16}")
    print(f"\n进程级 RSS 峰值（含建图与全部对象）: {rss_mb():.1f} MB")


if __name__ == "__main__":
    main()

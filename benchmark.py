"""性能基准：100 万条边、深度 1 万的图，输出耗时与内存峰值。

图结构：0->1->...->9999 的深链 + 随机边补足到 1,000,000 条（含重复与自环，
去重在 build_graph 内完成）。查询：start=0, max_depth=10000。
"""

import json
import random
import resource
import time
import tracemalloc

from recquery import build_graph, query

N_EDGES = 1_000_000
CHAIN = 10_000
N_NODES = 300_000
SEED = 42


def gen_edges():
    edges = [(i, i + 1) for i in range(CHAIN - 1)]
    rng = random.Random(SEED)
    while len(edges) < N_EDGES:
        edges.append((rng.randrange(N_NODES), rng.randrange(N_NODES)))
    return edges


def main():
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    tracemalloc.start()

    t0 = time.perf_counter()
    edges = gen_edges()
    t1 = time.perf_counter()
    graph, dups = build_graph(edges)
    t2 = time.perf_counter()
    result = query(graph, 0, CHAIN)
    t3 = time.perf_counter()

    py_current, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    metrics = {
        "edges_raw": N_EDGES,
        "duplicates_removed": dups,
        "nodes": len(graph),
        "chain_depth": CHAIN,
        "max_depth": CHAIN,
        "status": result["status"],
        "visited": len(result["visited"]),
        "truncated": len(result["truncated"]),
        "cycle_regions": len(result["cycles"]),
        "cycle_nodes_total": sum(len(r["nodes"]) for r in result["cycles"]),
        "time_generate_s": round(t1 - t0, 3),
        "time_build_graph_s": round(t2 - t1, 3),
        "time_query_s": round(t3 - t2, 3),
        "time_total_s": round(t3 - t0, 3),
        "mem_python_peak_mb": round(py_peak / 1024 / 1024, 1),
        "mem_rss_peak_mb": round(rss1 / 1024, 1),
        "mem_rss_delta_mb": round((rss1 - rss0) / 1024, 1),
    }
    with open("benchmark_result.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

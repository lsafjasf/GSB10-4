"""基准：100 万条边、深度 1 万的图，报告耗时与内存峰值。

图结构（确定性生成）
--------------------
- 主链 0 -> 1 -> ... -> 10000，根 0 查询、max_depth=10000 时
  末节点 10000 出边的后继被深度限制裁剪（Truncation）。
- 浅层探针：节点 2 加自环；保证大规模下环检测真实触发。
- 99 万余条确定性伪随机 DAG 边（i -> j, i < j），均为前向边，
  不会意外制造环。
- 额外放一个从根不可达的三环，验证“无可达路径”处理。
- 故意重复插入若干边，验证去重计数。
"""

import gc
import json
import os
import resource
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cycle_query import Graph, find_all_cycles, recursive_query

CHAIN_DEPTH = 10_000
TARGET_UNIQUE_EXTRA = 1_001_000  # 加主链与探针、去掉重后边，去重后约 100 万
SEED = 20261005
DUPLICATE_PROBES = 10


def gen_edges():
    n = CHAIN_DEPTH + 1
    for i in range(CHAIN_DEPTH):
        yield (i, i + 1)
    yield (2, 2)  # 浅层自环探针
    yield (CHAIN_DEPTH, CHAIN_DEPTH + 4)  # 深度限制探针：深度 10001 处被裁剪
    # 不可达三环（节点编号大于任何前向边终点）
    yield (n, n + 1)
    yield (n + 1, n + 2)
    yield (n + 2, n)

    state = SEED
    produced = 0
    while produced < TARGET_UNIQUE_EXTRA:
        state = (state * 6364136223846793005 + 1442695040888963407) & (2**64 - 1)
        i = state % n
        state = (state * 6364136223846793005 + 1442695040888963407) & (2**64 - 1)
        j = state % n
        if i < j:
            yield (i, j)
            produced += 1
            if produced % 100_000 < DUPLICATE_PROBES:
                yield (i, j)  # 重复边探针


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0  # KB -> MB


def main():
    gc.collect()
    t0 = time.perf_counter()
    g = Graph(gen_edges(), track_duplicates=True, auto_freeze=False)
    g.freeze()
    t1 = time.perf_counter()
    gc.collect()
    rss_after_build = peak_rss_mb()

    tracemalloc.start()
    t2 = time.perf_counter()
    res = recursive_query(g, 0, max_depth=CHAIN_DEPTH)
    t3 = time.perf_counter()
    _, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    t4 = time.perf_counter()
    all_cycles = find_all_cycles(g)
    t5 = time.perf_counter()
    rss_peak = peak_rss_mb()

    report = {
        "graph": {
            "edges_after_dedup": g.num_edges,
            "duplicates_skipped": g.duplicates_skipped,
            "nodes": len(g.adj),
            "chain_depth": CHAIN_DEPTH,
        },
        "recursive_query": {
            "max_depth": CHAIN_DEPTH,
            "shape": res.shape,
            "emitted_nodes": len(res.rows) - len(res.cycles) - len(res.truncated),
            "cycles": [list(c.nodes) for c in res.cycles],
            "truncated": [
                {"node": t.node, "depth": t.at_depth} for t in res.truncated
            ],
        },
        "find_all_cycles": {
            "cycles": [list(c) for c in all_cycles],
            "seconds": round(t5 - t4, 3),
        },
        "timing_seconds": {
            "build_graph": round(t1 - t0, 3),
            "recursive_query": round(t3 - t2, 3),
            "total_query_only": round(t3 - t0, 3),
        },
        "memory_mb": {
            "peak_rss_after_build": round(rss_after_build, 1),
            "peak_rss_total": round(rss_peak, 1),
            "python_alloc_peak_during_query": round(py_peak / 2**20, 1),
        },
        "python": sys.version.split()[0],
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

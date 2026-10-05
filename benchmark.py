#!/usr/bin/env python3
"""benchmark.py -- 10 万点 / 50 万边基准（仅标准库）。

图模型（种植划分, planted partition）:
    199 个社区 x 500 点, 社区内每点约 8 条边 (共 398000 条),
    跨社区随机边 102000 条, 另有 500 个孤立点 (节点 99500..99999),
    合计 n = 100000, m = 500000。

测量:
    * 构图 / 检测 分阶段耗时 (time.perf_counter)
    * 进程峰值内存 resource.getrusage(RUSAGE_SELF).ru_maxrss (Linux 单位 KB)
    * 同一种子连跑两次, 逐位对拍划分
    * 与种植真值计算 ARI, 验证大规模下结果仍有意义

用法: python3 benchmark.py [--seed 42]
"""
import os
import random
import resource
import sys
import time
from array import array

import community as C
from selftest import ari, sha256_of

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

N = 100_000
N_COMM = 199          # 199 个真实社区
COMM_SIZE = 500       # 每社区 500 点 -> 99500 点
N_ISO = N - N_COMM * COMM_SIZE  # 500 个孤立点
IN_PER_COMM = 2000    # 社区内边数 -> 199*2000 = 398000
N_CROSS = 102_000     # 跨社区边 -> 总计 500000


def gen_edges(seed=2024):
    rng = random.Random(seed)
    pairs = [(i, j) for i in range(COMM_SIZE) for j in range(i + 1, COMM_SIZE)]
    edges = array("i")
    planted = [0] * N
    for c in range(N_COMM):
        base = c * COMM_SIZE
        for i, j in rng.sample(pairs, IN_PER_COMM):
            edges.append(base + i)
            edges.append(base + j)
        for u in range(base, base + COMM_SIZE):
            planted[u] = c
    # 跨社区边（用单个整数编码去重，省内存）
    seen = set()
    while len(seen) < N_CROSS:
        u = rng.randrange(N_COMM * COMM_SIZE)
        v = rng.randrange(N_COMM * COMM_SIZE)
        if u == v or u // COMM_SIZE == v // COMM_SIZE:
            continue
        if u > v:
            u, v = v, u
        key = u * N + v
        if key not in seen:
            seen.add(key)
            edges.append(u)
            edges.append(v)
    # 孤立点：各自一个种植标签
    for u in range(N_COMM * COMM_SIZE, N):
        planted[u] = N_COMM + (u - N_COMM * COMM_SIZE)
    return edges, planted


def main():
    seed = 42
    if "--seed" in sys.argv:
        seed = int(sys.argv[sys.argv.index("--seed") + 1])

    lines = []
    def log(s):
        print(s)
        lines.append(s)

    log("# benchmark: n=%d, m=%d (planted: %d comms x %d + %d isolated)" % (
        N, N_COMM * IN_PER_COMM + N_CROSS, N_COMM, COMM_SIZE, N_ISO))
    log("# seed=%d  python=%s" % (seed, sys.version.split()[0]))

    t0 = time.perf_counter()
    edges, planted = gen_edges()
    t1 = time.perf_counter()
    log("gen_edges:        %8.2f s  (%d edges)" % (t1 - t0, len(edges) // 2))

    g = C.build_csr_arrays(N, edges)
    del edges
    t2 = time.perf_counter()
    log("build_csr:        %8.2f s" % (t2 - t1))

    labels1, info1 = C.louvain(g, seed=seed)
    t3 = time.perf_counter()
    log("detect run1:      %8.2f s  (levels=%d, passes=%s)" % (
        t3 - t2, info1["levels"], info1["passes_per_level"]))

    labels2, info2 = C.louvain(g, seed=seed)
    t4 = time.perf_counter()
    log("detect run2:      %8.2f s" % (t4 - t3))

    same = labels1 == labels2
    log("dup check:        %s  (sha run1=%s run2=%s)" % (
        "IDENTICAL" if same else "MISMATCH", sha256_of(labels1), sha256_of(labels2)))

    q = C.modularity(g, labels1)
    log("communities:      %d (planted %d + %d isolated)" % (
        info1["num_communities"], N_COMM, N_ISO))
    log("modularity:       %.6f" % q)
    log("ARI vs planted:   %.4f" % ari(labels1, planted))

    iso_found = sum(1 for u in range(N_COMM * COMM_SIZE, N)
                    if labels1.count(labels1[u]) == 1)
    # labels1.count 是 O(n)，改用集合统计
    from collections import Counter
    cnt = Counter(labels1)
    iso_found = sum(1 for u in range(N_COMM * COMM_SIZE, N) if cnt[labels1[u]] == 1)
    log("isolated nodes as singletons: %d / %d" % (iso_found, N_ISO))

    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    log("peak RSS:         %8.1f MB (ru_maxrss)" % (peak_kb / 1024.0))
    log("total wall:       %8.2f s" % (t4 - t0))

    with open(os.path.join(OUT, "benchmark.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(OUT, "partition_benchmark_run1.txt"), "w") as f:
        f.write("\n".join(map(str, labels1)) + "\n")
    with open(os.path.join(OUT, "partition_benchmark_run2.txt"), "w") as f:
        f.write("\n".join(map(str, labels2)) + "\n")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())

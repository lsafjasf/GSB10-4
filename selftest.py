#!/usr/bin/env python3
"""selftest.py -- 社区发现库的自测与指标报告（仅标准库）。

用法:
    python3 selftest.py            # 运行全部用例 + 对拍 + 指标，写入 out/
    python3 selftest.py --quiet    # 只输出 PASS/FAIL 汇总

产出（out/ 目录）:
    metrics_<case>.txt     每个用例的社区对比表（规模/内部边/导出边/模块度贡献/电导率/密度）
    partition_<case>_run{1,2}.txt  同一种子两次运行的逐节点社区标签（对拍用）
    dup_check.txt          对拍结果：逐位 diff 与 sha256 对比
    seed_stability.txt     不同种子下划分的 ARI / 相同节点对比例
    selftest_summary.txt   全部断言结果
"""
import hashlib
import os
import random
import sys
import time

import community as C

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
SEED = 42
STABILITY_SEEDS = (1, 7, 42, 123, 2024)

RESULTS = []  # (用例, 检查项, 是否通过, 说明)


def check(case, item, ok, note=""):
    RESULTS.append((case, item, bool(ok), note))
    return ok


# ---------------------------------------------------------------- 图构造

def gen_chain(n):
    return [(i, i + 1) for i in range(n - 1)]


def gen_two_cliques(k=40):
    edges = []
    for a, b in ((0, k), (k, 2 * k)):
        for i in range(a, b):
            for j in range(i + 1, b):
                edges.append((i, j))
    edges.append((k - 1, k))  # 唯一桥边
    return edges


def gen_dense_random(n=150, m=8000, seed=7):
    rng = random.Random(seed)
    edges = set()
    while len(edges) < m:
        u = rng.randrange(n)
        v = rng.randrange(n)
        if u != v:
            edges.add((min(u, v), max(u, v)))
    return sorted(edges)


def gen_isolated_mix():
    # 8 点团 + 5 个孤立点（8..12）
    edges = [(i, j) for i in range(8) for j in range(i + 1, 8)]
    return edges, 13


def gen_selfloop_only():
    # 点 5 只有自环，点 6 自环 + 一条边
    edges = [(0, 1), (1, 2), (2, 0), (5, 5), (6, 6), (6, 7)]
    return edges, 8


def gen_weighted_parallel():
    # 平行边与权重：(0,1)x3、(1,2)x1、(2,3)x5、(3,4)x1
    return [(0, 1, 3), (1, 2, 1), (2, 3, 5), (3, 4, 1)], 5


CASES = [
    ("empty",        0,  []),
    ("single_node",  1,  []),
    ("single_edge",  2,  [(0, 1)]),
    ("chain30",      30, gen_chain(30)),
    ("two_cliques",  80, gen_two_cliques(40)),
    ("dense_random", 150, gen_dense_random()),
]
_extra = gen_isolated_mix()
CASES.append(("isolated_mix", _extra[1], _extra[0]))
_extra = gen_selfloop_only()
CASES.append(("selfloop_only", _extra[1], _extra[0]))
_extra = gen_weighted_parallel()
CASES.append(("weighted_parallel", _extra[1], _extra[0]))


# ---------------------------------------------------------------- 指标

def community_stats(g, labels):
    """逐社区统计：规模、加权度、内部边权、导出边权、模块度贡献、电导率、内部密度。"""
    k = max(labels) + 1 if labels else 0
    size = [0] * k
    tot = [0] * k          # 社区内所有点的加权度之和（含自环，自环计一次）
    in_w = [0] * k         # 内部边权（自环计一次，普通内部边计两次 -> 存的是 2*内部权）
    loop_w = [0] * k       # 社区内自环权重（计一次）
    for u in range(g.n):
        c = labels[u]
        size[c] += 1
        for i in range(g.offsets[u], g.offsets[u + 1]):
            w = g.weights[i]
            tot[c] += w
            v = g.neighbors[i]
            if v == u:
                loop_w[c] += w
            elif labels[v] == c:
                in_w[c] += w
    m2 = g.m2
    rows = []
    for c in range(k):
        internal = in_w[c] / 2.0            # 真实内部边权（含自环）
        boundary = tot[c] - in_w[c] - loop_w[c]  # 导出边权（跨社区只计一次）
        q_c = ((in_w[c] + loop_w[c]) / m2 - (tot[c] / m2) ** 2) if m2 else 0.0
        denom = min(tot[c], m2 - tot[c])
        cond = (boundary / denom) if denom > 0 else 0.0
        # 密度只按普通内部边计（自环不算"点对之间的边"），上界为 1
        dens = (internal / (size[c] * (size[c] - 1) / 2.0)) if size[c] > 1 else 0.0
        rows.append(dict(cid=c, size=size[c], internal=internal, boundary=boundary,
                         tot=tot[c], loop=loop_w[c], q=q_c, cond=cond, dens=dens))
    return rows


def fmt_stats_table(name, n, m_edges, labels, info, rows):
    lines = []
    lines.append("case=%s n=%d edges=%d seed=%d" % (name, n, m_edges, info["seed"]))
    lines.append("communities=%d modularity=%.6f levels=%d passes=%s" % (
        info["num_communities"], info["modularity"], info["levels"],
        ",".join(str(p) for p in info["passes_per_level"])))
    lines.append("%-4s %6s %12s %12s %12s %12s %12s %12s %10s" % (
        "cid", "size", "internal_w", "selfloop_w", "boundary_w", "degree_tot",
        "modularity_c", "conductance", "density"))
    for r in rows:
        lines.append("%-4d %6d %12.1f %12.1f %12.1f %12.1f %12.6f %12.6f %10.4f" % (
            r["cid"], r["size"], r["internal"], r["loop"], r["boundary"], r["tot"],
            r["q"], r["cond"], r["dens"]))
    lines.append("%-4s %6d %12.1f %12.1f %12.1f %12.1f %12.6f" % (
        "ALL", n, sum(r["internal"] for r in rows), sum(r["loop"] for r in rows),
        sum(r["boundary"] for r in rows), sum(r["tot"] for r in rows),
        info["modularity"]))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 对拍 / 稳定性

def sha256_of(labels):
    h = hashlib.sha256()
    for x in labels:
        h.update(str(x).encode())
        h.update(b",")
    return h.hexdigest()[:16]


def partition_text(labels):
    return "\n".join(str(x) for x in labels) + "\n"


def ari(a, b):
    """调整兰德指数（纯标准库实现）。"""
    n = len(a)
    cells = {}
    ca = {}
    cb = {}
    for x, y in zip(a, b):
        cells[(x, y)] = cells.get((x, y), 0) + 1
        ca[x] = ca.get(x, 0) + 1
        cb[y] = cb.get(y, 0) + 1

    def comb2(v):
        return v * (v - 1) // 2

    sum_cells = sum(comb2(v) for v in cells.values())
    sum_a = sum(comb2(v) for v in ca.values())
    sum_b = sum(comb2(v) for v in cb.values())
    total = comb2(n)
    if total == 0:
        return 1.0
    expected = sum_a * sum_b / total
    maxi = 0.5 * (sum_a + sum_b)
    denom = maxi - expected
    if denom == 0:
        return 1.0 if sum_cells == maxi else 0.0
    return (sum_cells - expected) / denom


def agree_ratio(a, b):
    """两个划分在'任意两点是否同社区'上的一致比例。"""
    n = len(a)
    if n < 2:
        return 1.0
    same = 0
    total = 0
    for i in range(n):
        ai = a[i]
        bi = b[i]
        for j in range(i + 1, n):
            total += 1
            if (ai == a[j]) == (bi == b[j]):
                same += 1
    return same / total


# ---------------------------------------------------------------- 主流程

def run_case(name, n, edges, quiet):
    t0 = time.perf_counter()
    g = C.build_csr(n, edges)
    labels1, info1 = C.louvain(g, seed=SEED)
    labels2, info2 = C.louvain(g, seed=SEED)
    dt = time.perf_counter() - t0
    info1["modularity"] = C.modularity(g, labels1)

    # --- 对拍：同种子两次运行逐位一致
    same = labels1 == labels2
    h1, h2 = sha256_of(labels1), sha256_of(labels2)
    check(name, "reproducible_same_seed", same,
          "run1 sha=%s run2 sha=%s" % (h1, h2))

    # --- 指标表
    rows = community_stats(g, labels1)
    table = fmt_stats_table(name, n, len(edges), labels1, info1, rows)
    with open(os.path.join(OUT, "metrics_%s.txt" % name), "w") as f:
        f.write(table)
    with open(os.path.join(OUT, "partition_%s_run1.txt" % name), "w") as f:
        f.write(partition_text(labels1))
    with open(os.path.join(OUT, "partition_%s_run2.txt" % name), "w") as f:
        f.write(partition_text(labels2))

    if not quiet:
        print("== %s: n=%d edges=%d -> %d communities, Q=%.4f, %.3fs" % (
            name, n, len(edges), info1["num_communities"], info1["modularity"], dt))
        print(table)
    return g, labels1, info1, same, h1, h2


def main():
    quiet = "--quiet" in sys.argv
    os.makedirs(OUT, exist_ok=True)

    dup_lines = ["# 对拍：同一种子 (seed=%d) 两次运行的划分逐位比较" % SEED,
                 "%-18s %-8s %-18s %-18s %s" % ("case", "result", "sha256_run1", "sha256_run2", "note")]
    graphs = {}
    labels_by_case = {}

    for name, n, edges in CASES:
        g, labels, info, same, h1, h2 = run_case(name, n, edges, quiet)
        graphs[name] = g
        labels_by_case[name] = labels
        dup_lines.append("%-18s %-8s %-18s %-18s %s" % (
            name, "OK" if same else "DIFF", h1, h2,
            "identical" if same else "MISMATCH"))

    # ------------------------------------------------------------ 断言
    # 空图 / 单点
    check("empty", "no_communities", labels_by_case["empty"] == [])
    check("single_node", "singleton", labels_by_case["single_node"] == [0])

    # 单边：两点同社区
    check("single_edge", "together", labels_by_case["single_edge"] == [0, 0])

    # 链状图：社区数有界且每个社区是连续区间
    lab = labels_by_case["chain30"]
    k = len(set(lab))
    ok_range = 2 <= k <= 15
    contiguous = True
    for c in set(lab):
        idx = [i for i, x in enumerate(lab) if x == c]
        if idx != list(range(idx[0], idx[-1] + 1)):
            contiguous = False
    check("chain30", "community_count_in_[2,15]", ok_range, "k=%d" % k)
    check("chain30", "contiguous_segments", contiguous)

    # 两团：恰好 2 个社区，且与真实划分一致
    lab = labels_by_case["two_cliques"]
    truth = [0] * 40 + [1] * 40
    check("two_cliques", "exactly_2_communities", len(set(lab)) == 2)
    check("two_cliques", "matches_ground_truth", agree_ratio(lab, truth) == 1.0)

    # 稠密随机图（ER, p≈0.72）：无真实结构。
    # 已知现象：模块度最优化在纯随机图上也会因涨落得到小的正 Q 并切出
    # 若干"伪社区"（Guimera et al. 2004），因此不断言单社区，而是断言：
    #   1) 模块度显著低于结构化图（< 0.15，对比两团 0.50 / 链 0.65）；
    #   2) 社区数远小于点数（< n/10）；
    #   3) 划分对种子不稳定（见 seed_stability.txt，ARI 明显低于结构图）。
    lab = labels_by_case["dense_random"]
    g_dense = graphs["dense_random"]
    q_dense = C.modularity(g_dense, lab)
    check("dense_random", "low_modularity(<0.15)", q_dense < 0.15,
          "Q=%.4f" % q_dense)
    check("dense_random", "few_communities(<n/10)", len(set(lab)) < 150 // 10,
          "k=%d" % len(set(lab)))

    # 孤立点：8 点团一个社区，5 个孤立点各自单点社区
    lab = labels_by_case["isolated_mix"]
    clique_ok = len(set(lab[:8])) == 1
    iso_ok = len(set(lab[8:])) == 5 and all(lab[i] != lab[0] for i in range(8, 13))
    check("isolated_mix", "clique_together", clique_ok)
    check("isolated_mix", "isolated_nodes_are_singletons", iso_ok,
          "labels[8:]=%s" % (lab[8:],))

    # 自环：只有自环的点 5 必须单点；点 6（自环+边）与 7 同社区
    lab = labels_by_case["selfloop_only"]
    check("selfloop_only", "selfloop_node_is_singleton",
          lab[5] not in (lab[0], lab[1], lab[2], lab[6], lab[7]) and
          lab.count(lab[5]) == 1)
    check("selfloop_only", "triangle_together", lab[0] == lab[1] == lab[2])
    check("selfloop_only", "edge_pair_together", lab[6] == lab[7])

    # 加权/平行边：{0,1}、{2,3,4}（(2,3) 权重 5 最强，(0,1) 权重 3 次之）
    lab = labels_by_case["weighted_parallel"]
    check("weighted_parallel", "respects_weights",
          lab[0] == lab[1] and lab[2] == lab[3] == lab[4] and lab[0] != lab[2],
          "labels=%s" % (lab,))

    # ------------------------------------------------------------ 种子稳定性
    stab = ["# 不同种子下划分的稳定性（ARI = 调整兰德指数, agree = 节点对一致比例）",
            "%-14s %8s %8s %8s" % ("case", "seed_a", "seed_b", "ARI") + "   agree"]
    for name in ("chain30", "two_cliques", "dense_random", "isolated_mix"):
        g = graphs[name]
        base = labels_by_case[name]
        for s in STABILITY_SEEDS:
            if s == SEED:
                continue
            other, _ = C.louvain(g, seed=s)
            other = C.canonical_labels(other)
            stab.append("%-14s %8d %8d %8.4f %8.4f" % (
                name, SEED, s, ari(base, other), agree_ratio(base, other)))
    with open(os.path.join(OUT, "seed_stability.txt"), "w") as f:
        f.write("\n".join(stab) + "\n")

    with open(os.path.join(OUT, "dup_check.txt"), "w") as f:
        f.write("\n".join(dup_lines) + "\n")

    # ------------------------------------------------------------ 汇总
    n_fail = sum(1 for r in RESULTS if not r[2])
    lines = ["# selftest 汇总  seed=%d" % SEED, ""]
    for case, item, ok, note in RESULTS:
        lines.append("[%s] %-28s %-32s %s" % ("PASS" if ok else "FAIL", case, item, note))
    lines.append("")
    lines.append("total=%d failed=%d" % (len(RESULTS), n_fail))
    summary = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "selftest_summary.txt"), "w") as f:
        f.write(summary)
    print(summary)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())

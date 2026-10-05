"""自测 + 对拍：固定边界情形 + 大规模随机图逐点对拍参考实现。

运行：python3 src/diff_test.py
"""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(__file__))

from negsp import shortest_paths, INF, NEG_INF                       # noqa: E402
from reference import reference                                      # noqa: E402


def reachable_from(n, edges, src):
    adj = [[] for _ in range(n)]
    for u, v, _ in edges:
        adj[u].append(v)
    seen = {src}
    stack = [src]
    while stack:
        u = stack.pop()
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return seen


def validate_cycle(cyc, edges, reach_set, label):
    """环的独立校验：顺序成环、每段边真实存在、总权严格为负、可达性分类正确。"""
    wmin = {}
    for u, v, w in edges:
        key = (u, v)
        if key not in wmin or w < wmin[key]:
            wmin[key] = w
    k = len(cyc.nodes)
    assert len(set(cyc.nodes)) == k, f"{label}: 环上节点重复 {cyc.nodes}"
    total = 0
    for i in range(k):
        a, b = cyc.nodes[i], cyc.nodes[(i + 1) % k]
        assert (a, b) in wmin, f"{label}: 环上不存在边 {a}->{b}"
        total += wmin[(a, b)]
    assert total < 0, f"{label}: 环总权 {total} 不是负数"
    expected_reach = any(node in reach_set for node in cyc.nodes)
    assert cyc.reachable_from_source == expected_reach, (
        f"{label}: 可达性分类错误 {cyc.reachable_from_source} != {expected_reach}")


def compare(n, edges, src, label):
    """被测实现 vs 参考实现逐点对拍。"""
    res = shortest_paths(n, edges, src)
    ref = reference(n, edges, src)
    reach_set = reachable_from(n, edges, src)

    # 1) 负环存在性（全图 / 可达）必须一致
    assert res.has_negative_cycle == ref.has_any_cycle, (
        f"{label}: 全图负环判定不一致 {res.has_negative_cycle} vs {ref.has_any_cycle}")
    got_reach = any(c.reachable_from_source for c in res.negative_cycles)
    assert got_reach == ref.has_reachable_cycle, (
        f"{label}: 可达负环判定不一致 {got_reach} vs {ref.has_reachable_cycle}")

    # 2) 每个报告的环都要真实合法
    for c in res.negative_cycles:
        validate_cycle(c, edges, reach_set, label)

    # 3) 逐点距离对拍（inf / -inf / 有限值 三类）
    for v in range(n):
        got, want = res.dist[v], ref.dist[v]
        if want == 'inf':
            assert got is INF, f"{label}: 点{v} 应为 inf，实际 {got}"
        elif want == '-inf':
            assert got == NEG_INF, f"{label}: 点{v} 应为 -inf，实际 {got}"
        else:
            assert got != INF and got != NEG_INF and abs(got - float(want)) < 1e-9, (
                f"{label}: 点{v} 距离不一致 {got} vs {want}")

    # 4) 参考认定有可达负环时，被测必须至少定位出一个可达负环
    if ref.has_reachable_cycle:
        assert res.reachable_cycles, f"{label}: 参考发现可达负环但未定位出环"
    return res


def random_graph(rng):
    n = rng.randint(2, 22)
    m = rng.randint(0, 3 * n)
    edges = []
    for _ in range(m):
        u = rng.randrange(n)
        v = rng.randrange(n)
        w = rng.randint(-8, 15)          # 允许负边、零边、自环、重边
        edges.append((u, v, w))
    return n, edges, rng.randrange(n)


# ---------------------------------------------------------------- 固定情形
FIXED = []


def case(name, n, edges, src):
    FIXED.append((name, n, edges, src))


# 1. 全正权
case("全正权", 4, [(0, 1, 4), (0, 2, 1), (2, 1, 2), (1, 3, 1), (2, 3, 5)], 0)
# 2. 含负权边但无环（DAG）
case("含负权无环", 5, [(0, 1, 4), (0, 2, 5), (1, 2, -3), (1, 3, 6),
                       (2, 3, 4), (2, 4, 2), (3, 4, -2)], 0)
# 3. 可达负环：1->2->1 权 -1
case("可达负环", 4, [(0, 1, 0), (1, 2, -2), (2, 1, 1), (1, 3, 100)], 0)
# 4. 不可达负环：源点在 {0}，负环在 {2,3} 内
case("不可达负环", 4, [(0, 1, 2), (2, 3, -2), (3, 2, 1)], 0)
# 5. 零权环：1->2->1 权和 0
case("零权环", 3, [(0, 1, 1), (1, 2, -1), (2, 1, 1)], 0)
# 6. 零权自环
case("零权自环", 2, [(0, 1, 3), (1, 1, 0)], 0)
# 7. 正权自环
case("正权自环", 2, [(0, 1, 3), (1, 1, 5)], 0)
# 8. 负自环（长度 1 的负环）
case("负自环", 2, [(0, 1, 3), (1, 1, -4)], 0)
# 9. 同时含可达负环与不可达负环
case("可达+不可达负环", 6, [(0, 1, 0), (1, 2, -2), (2, 1, 1),
                            (3, 4, -1), (4, 5, -1), (5, 3, -1)], 0)
# 10. 不可达负环 + 其环外有到可达点的边（验证超级源不串扰）
case("不可达环指向可达点", 4, [(0, 1, 10), (2, 3, -2), (3, 2, 1), (2, 1, 1)], 0)
# 11. 不连通且完全正常的第二分量（零权环）
case("不可达零权环", 4, [(0, 1, 2), (2, 3, -3), (3, 2, 3)], 0)


def main():
    print("== 固定情形 ==")
    for name, n, edges, src in FIXED:
        res = compare(n, edges, src, name)
        if res.negative_cycles:
            for c in res.negative_cycles:
                tag = "源可达" if c.reachable_from_source else "源不可达"
                print(f"  [{name}] 负环节点={list(c.nodes)} 环权={c.total_weight} ({tag})")
        else:
            print(f"  [{name}] 无负环  dist={[None if d == INF else d for d in res.dist]}")

    print("== 随机图逐点对拍 ==")
    trials = int(os.environ.get("DIFF_TRIALS", "3000"))
    rng = random.Random(20261005)
    n_with_cycle = n_with_unreach = n_neg_edges = 0
    for t in range(trials):
        n, edges, src = random_graph(rng)
        res = compare(n, edges, src, f"random#{t}")
        if res.has_negative_cycle:
            n_with_cycle += 1
        if any(not c.reachable_from_source for c in res.negative_cycles):
            n_with_unreach += 1
        if any(w < 0 for _, _, w in edges) and not res.has_negative_cycle:
            n_neg_edges += 1
    print(f"  随机组数={trials}  含负环组数={n_with_cycle}  "
          f"其中含不可达负环={n_with_unreach}  有负边但无环={n_neg_edges}")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()

"""单源最短路径 + 负环检测（Bellman-Ford，仅用 Python 标准库）。

核心结论
--------
1. 负权边：存在边 (u, v, w) 满足 w < 0。负权边本身合法，最短路径仍然有定义。
2. 负环：存在有向环 C 使环上权值之和 sum(w) < 0。负环才是使最短路径
   无定义（-inf）的唯一原因：沿环每绕一圈，路径权值严格减小，可无限绕。
3. 判定依据：Bellman-Ford 做 n-1 轮松弛后，任意从源点可达的最短路径都已确定
   （简单路径最多 n-1 条边）；若第 n 轮仍能松弛某个点，则该点的前驱链中必含
   一个从源可达的负环（CLRS 第 24 章定理）。
4. 不可达负环：从源点不可达的点不会被松弛，需引入“超级源”——把所有不可达点
   的距离初始化为 0，等价于从一个连向所有点的虚拟源再做一次检测。
5. 松弛条件是严格小于（du + w < dv），故零权环 / 零权自环 / 正权自环都不会
   触发判定；只有负自环（长度 1 的负环）会被判定。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

INF = math.inf
NEG_INF = -math.inf

Edge = Tuple[int, int, float]


@dataclass(frozen=True)
class NegativeCycle:
    """一个负环的定位结果。"""

    nodes: Tuple[int, ...]       # 环上的节点，按环顺序排列，首尾不重复
    total_weight: float          # 环上各边权值之和（严格 < 0）
    reachable_from_source: bool  # 该环是否存在从源点到环上任一点的路径


@dataclass
class Result:
    dist: List[float]                       # dist[v]：INF 不可达；NEG_INF 因可达负环而无定义
    negative_cycles: List[NegativeCycle] = field(default_factory=list)

    @property
    def has_negative_cycle(self) -> bool:
        return bool(self.negative_cycles)

    @property
    def reachable_cycles(self) -> List[NegativeCycle]:
        return [c for c in self.negative_cycles if c.reachable_from_source]

    @property
    def unreachable_cycles(self) -> List[NegativeCycle]:
        return [c for c in self.negative_cycles if not c.reachable_from_source]


def _relax_round(dist, pred, edges) -> bool:
    """一轮全边松弛，返回本轮是否有更新。"""
    updated = False
    for u, v, w in edges:
        du = dist[u]
        if du is not INF and du + w < dist[v]:
            dist[v] = du + w
            pred[v] = u
            updated = True
    return updated


def _extract_cycle(start, pred, n):
    """从第 n 轮仍可松弛的点出发，沿前驱链定位负环。

    先沿前驱走 n 步（保证进入环内，而不是停在通向环的链上），再收集环。
    """
    v = start
    for _ in range(n):
        p = pred[v]
        if p == -1:
            return None
        v = p
    cycle = [v]
    u = pred[v]
    guard = 0
    while u != v:
        if u == -1 or guard > n:
            return None
        cycle.append(u)
        u = pred[u]
        guard += 1
    cycle.reverse()
    return cycle


def _cycle_weight(cycle, weight_of):
    total = 0.0
    k = len(cycle)
    for i in range(k):
        total += weight_of[(cycle[i], cycle[(i + 1) % k])]
    return total


def shortest_paths(n: int, edges: Sequence[Edge], source: int) -> Result:
    """计算从 source 出发的单源最短路径，并检测全部负环。

    - 无负环：dist 为确定的最短距离。
    - 有从源可达的负环：环上及其下游点的 dist 置为 NEG_INF（最短路径无定义）。
    - 仅存在不可达负环：从源可达的点距离仍为正确的有限值。
    """
    if not (0 <= source < n):
        raise ValueError("source 越界")

    # 每对点取最小权边：定位出的环取该边也一定是负环（只可能更小）。
    weight_of = {}
    for u, v, w in edges:
        key = (u, v)
        old = weight_of.get(key)
        if old is None or w < old:
            weight_of[key] = w

    adj = [[] for _ in range(n)]
    for u, v, _w in edges:
        adj[u].append(v)

    # ---- 阶段 A：从真实源点做 Bellman-Ford -------------------------------
    dist = [INF] * n
    pred = [-1] * n
    dist[source] = 0

    for _ in range(n - 1):
        if not _relax_round(dist, pred, edges):
            break

    # 第 n 轮：仍可松弛的点位于某个“从源可达负环”上或其下游
    tainted = []
    for u, v, w in edges:
        du = dist[u]
        if du is not INF and du + w < dist[v]:
            pred[v] = u
            if not tainted or tainted[-1] != v:
                tainted.append(v)

    cycles: List[NegativeCycle] = []
    seen = set()
    for t in tainted:
        cyc = _extract_cycle(t, pred, n)
        if cyc is None:
            continue
        key = frozenset(cyc)
        if key in seen:
            continue
        seen.add(key)
        total = _cycle_weight(cyc, weight_of)
        if total < 0:  # 防御性校验：理论上必然成立
            cycles.append(NegativeCycle(tuple(cyc), total, True))

    # ---- 阶段 B：在“从源不可达”的点集上用超级源检测负环 ------------------
    if any(d is INF for d in dist):
        # 注意：不可达点可能有边指向可达点（如 9->1），因此必须把松弛
        # 限制在“两端点均不可达”的诱导子图上，否则可达点会被超级源串扰。
        # 含可达点的环其所有点都可从源到达，必已被阶段 A 发现，不会漏判。
        unreach = [d is INF for d in dist]
        edges_b = [(u, v, w) for u, v, w in edges if unreach[u] and unreach[v]]
        d2 = [0.0 if d is INF else INF for d in dist]
        p2 = [-1] * n
        for _ in range(n - 1):
            if not _relax_round(d2, p2, edges_b):
                break
        tainted2 = []
        for u, v, w in edges_b:
            du = d2[u]
            if du is not INF and du + w < d2[v]:
                p2[v] = u
                if not tainted2 or tainted2[-1] != v:
                    tainted2.append(v)
        for t in tainted2:
            cyc = _extract_cycle(t, p2, n)
            if cyc is None:
                continue
            key = frozenset(cyc)
            if key in seen:
                continue
            seen.add(key)
            total = _cycle_weight(cyc, weight_of)
            if total < 0:
                cycles.append(NegativeCycle(tuple(cyc), total, False))

    # ---- 从源可达负环出发，其下游所有点最短路径无定义（-inf）--------------
    q = deque()
    neg_set = set()
    for c in cycles:
        if c.reachable_from_source:
            for node in c.nodes:
                if node not in neg_set:
                    neg_set.add(node)
                    q.append(node)
    while q:
        u = q.popleft()
        for v in adj[u]:
            if v not in neg_set:
                neg_set.add(v)
                q.append(v)
    for v in neg_set:
        dist[v] = NEG_INF

    return Result(dist=dist, negative_cycles=cycles)

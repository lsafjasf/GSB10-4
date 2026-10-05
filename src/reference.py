"""参考实现（教科书 Bellman-Ford，刻意与 negsp.py 写法不同、互相独立）。

- 不做提前退出，老老实实跑满 n 轮；
- 距离用 Fraction（精确分数），避免浮点影响逐点对拍；
- 不做环提取，只给出“是否存在从源可达负环 / 图中是否存在任何负环”以及
  受负环影响（距离应为 -inf）的点集，供对拍独立核对。
"""

from fractions import Fraction
from collections import deque

INF_SENT = None  # 不可达


class Ref:
    def __init__(self, dist, has_reachable_cycle, has_any_cycle, neg_nodes):
        self.dist = dist                       # Fraction / 'inf' / '-inf'
        self.has_reachable_cycle = has_reachable_cycle
        self.has_any_cycle = has_any_cycle
        self.neg_nodes = neg_nodes             # 距离无定义（-inf）的点


def _bf(n, edges, init, rounds):
    dist = list(init)
    updated_last = False
    for _ in range(rounds):
        updated_last = False
        for u, v, w in edges:
            du = dist[u]
            dv = dist[v]
            if du is not INF_SENT and (dv is INF_SENT or du + w < dv):
                dist[v] = du + w
                updated_last = True
        if not updated_last:
            break
    return dist, updated_last


def reference(n, edges, source):
    f_edges = [(u, v, Fraction(w)) for u, v, w in edges]

    # 从源点：跑 n 轮（n-1 轮定距离，第 n 轮判定可达负环）
    init = [INF_SENT] * n
    init[source] = Fraction(0)
    dist, changed = _bf(n, f_edges, init, n)
    has_reachable = changed

    adj = [[] for _ in range(n)]
    for u, v, _ in f_edges:
        adj[u].append(v)

    # 再跑一轮，收集仍可松弛的点作为 -inf 种子（位于环上或环下游）
    seeds = set()
    for u, v, w in f_edges:
        du = dist[u]
        dv = dist[v]
        if du is not INF_SENT and (dv is INF_SENT or du + w < dv):
            seeds.add(v)
    neg_nodes = set(seeds)
    q = deque(seeds)
    while q:
        u = q.popleft()
        for v in adj[u]:
            if v not in neg_nodes:
                neg_nodes.add(v)
                q.append(v)

    out = ['-inf' if v in neg_nodes else ('inf' if dist[v] is INF_SENT else dist[v])
           for v in range(n)]

    # 全图负环：超级源（所有点初始 0），跑 n 轮
    d2, changed2 = _bf(n, f_edges, [Fraction(0)] * n, n)
    has_any = changed2

    return Ref(out, has_reachable, has_any, neg_nodes)

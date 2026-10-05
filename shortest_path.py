"""Bellman-Ford 最短路径与负环检测（仅标准库）。

判定条件与依据
==============
1. 负权边：单独一条边 (u, v, w) 满足 w < 0 即为负权边。
   负权边本身合法，只表示沿该边代价下降，不破坏最短路径的存在性。

2. 负环（负权回路）：环上所有边权之和 < 0。
   依据：若环从源点可达，则绕环每走一圈路径权值就减小 |环权|，
   可以无限绕圈使路径权值趋于 -inf，因此"最短路径"不存在（未定义）。
   算法判定：Bellman-Ford 在 n-1 轮松弛后，所有"边数 <= n-1"的最短路
   已确定；第 n 轮若仍有边 (u,v,w) 满足 dist[u]+w < dist[v]，
   当且仅当存在从源点可达的负环（v 在负环上，或可被负环到达）。

3. 不可达负环：源点不可达的负环不影响源点出发的最短路。
   用超级源点（所有点 dist=0 初始化，等价于虚拟源点向每点连零权边）
   做同样的第 n 轮检测，可判定图中是否存在"任意"负环，与可达性无关。

4. 零权环（环权和 == 0）与 w >= 0 的自环：不满足"环权 < 0"。
   松弛条件 dist[u]+w < dist[v] 是严格小于，绕零权环不会使 dist 下降，
   因此第 n 轮不会触发，不会被误判。w < 0 的自环是长度为 1 的负环。

工程优化（不改变上述判定的严格性）：
   负环存在时标准 BF 必须跑满 n 轮才能判定，最坏复杂度 O(n*m)。
   核心循环每 check_every 轮沿 parent 指针找一次"父指针环"，并直接用
   实际边权验证环权和 < 0；验证通过才提前返回（可靠：环权确为负）；
   验证不通过则继续，最终仍以第 n 轮松弛为准（完备）。
"""

from collections import namedtuple

INF = float("inf")

Edge = namedtuple("Edge", ["u", "v", "w"])

SSSPResult = namedtuple(
    "SSSPResult",
    [
        "dist",            # list[float] 源点到各点最短距离，不可达为 INF
        "has_neg_cycle",   # bool 是否存在从源点可达的负环
        "cycle",           # list[int] 负环上的节点（按环顺序），无负环为 None
        "cycle_reachable", # bool 检出的负环是否从源点可达
        "rounds",          # int 实际松弛轮数
    ],
)

_CHECK_EVERY = 8


def _extract_cycle_on_nth_round(n, parent, x):
    """第 n 轮仍被松弛的节点 x：沿 parent 回走 n 步必已进入环内，再收集整环。"""
    for _ in range(n):
        x = parent[x]
    cycle = [x]
    y = parent[x]
    while y != x:
        cycle.append(y)
        y = parent[y]
    cycle.reverse()
    return cycle


def _try_cycle_from_chain(n, parent, x):
    """沿 parent 链查找重复节点（父指针环），返回按环顺序排列的节点或 None。
    链 ..., a, b, c, a(重复) => 环 [a, b, c]（边 a->b,b->c,c->a 均为真实边）。"""
    seen = {}
    chain = []
    steps = 0
    while x != -1 and steps <= n:
        if x in seen:
            # 链为 parent 方向（边方向相反），反转后才是环的边方向
            return chain[seen[x]:][::-1]
        seen[x] = steps
        chain.append(x)
        x = parent[x]
        steps += 1
    return None


def _edge_weight_map(edges):
    """{(u, v): 最小权}，用于对候选环做环权验证（平行边取最小）。"""
    wmap = {}
    for u, v, w in edges:
        key = (u, v)
        old = wmap.get(key)
        if old is None or w < old:
            wmap[key] = w
    return wmap


def _cycle_sum(cycle, wmap):
    m = len(cycle)
    total = 0
    for i in range(m):
        total += wmap[(cycle[i], cycle[(i + 1) % m])]
    return total


def _bellman_ford_core(n, edges, dist):
    """核心松弛。返回 (witness, parent, cycle_or_None, rounds)。"""
    parent = [-1] * n
    wmap = None
    for i in range(n):
        updated = False
        witness = None
        for u, v, w in edges:
            du = dist[u]
            if du != INF and du + w < dist[v]:
                dist[v] = du + w
                parent[v] = u
                updated = True
                witness = v
        if not updated:
            return None, parent, None, i
        if i == n - 1:
            # Bellman-Ford 判定：第 n 轮仍可松弛 <=> 存在可达负环
            cycle = _extract_cycle_on_nth_round(n, parent, witness)
            return witness, parent, cycle, n
        if (i + 1) % _CHECK_EVERY == 0:
            candidate = _try_cycle_from_chain(n, parent, witness)
            if candidate is not None:
                if wmap is None:
                    wmap = _edge_weight_map(edges)
                if _cycle_sum(candidate, wmap) < 0:
                    return witness, parent, candidate, i + 1
    return None, parent, None, n


def bellman_ford(n, edges, source):
    """单源最短路径 + 源点可达负环检测。

    存在源点可达负环时 has_neg_cycle=True、cycle 为环上节点、
    cycle_reachable=True；此时 dist 无定义（可无限绕环趋 -inf）。
    """
    dist = [INF] * n
    dist[source] = 0
    witness, parent, cycle, rounds = _bellman_ford_core(n, edges, dist)
    if witness is None:
        return SSSPResult(dist, False, None, False, rounds)
    return SSSPResult(dist, True, cycle, True, rounds)


def find_any_negative_cycle(n, edges, return_rounds=False):
    """检测图中任意负环（无论是否从某源点可达），用超级源点初始化。
    返回负环节点列表；无负环返回 None。"""
    dist = [0] * n
    witness, parent, cycle, rounds = _bellman_ford_core(n, edges, dist)
    if witness is None:
        return (None, rounds) if return_rounds else None
    return (cycle, rounds) if return_rounds else cycle


def has_negative_weight_edge(edges):
    """负权边判定：存在 w < 0 的边。与负环判定相互独立。"""
    return any(w < 0 for _, _, w in edges)


def cycle_weight(cycle, edges):
    """计算环的实际权值和（平行边取最小），用于验证检出的环确为负环。"""
    return _cycle_sum(cycle, _edge_weight_map(edges))


def spfa_reference(n, edges, source):
    """参考实现：SPFA（队列优化 Bellman-Ford），用于对拍。
    假定不存在源点可达的负环。返回 dist 数组。"""
    from collections import deque

    adj = [[] for _ in range(n)]
    for u, v, w in edges:
        adj[u].append((v, w))
    dist = [INF] * n
    dist[source] = 0
    inq = [False] * n
    q = deque([source])
    inq[source] = True
    while q:
        u = q.popleft()
        inq[u] = False
        for v, w in adj[u]:
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                if not inq[v]:
                    q.append(v)
                    inq[v] = True
    return dist

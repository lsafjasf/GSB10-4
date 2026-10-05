"""重排策略：natural / RCM / minimum-degree。

均返回 perm：perm[新编号] = 旧编号，供 SparseMatrix.permute_symmetric 使用。
对称重排保持对角元位置不变，因此不需要部分选主元即可安全分解。
"""

import heapq
from collections import deque


def natural_order(n):
    return list(range(n))


def rcm_order(matrix):
    """Reverse Cuthill-McKee：BFS 中按度从小到大访问，最后整体反转。

    目标是压缩带宽，使非零元集中在对角线附近，从而减少填充。
    """
    n = matrix.n
    adj = matrix.symmetrized_pattern()
    degree = [len(a) for a in adj]
    visited = [False] * n
    # 外层从未访问的最小度节点开始，尽量接近伪外围节点
    seeds = sorted(range(n), key=lambda v: (degree[v], v))
    order = []
    for start in seeds:
        if visited[start]:
            continue
        visited[start] = True
        queue = deque([start])
        while queue:
            v = queue.popleft()
            order.append(v)
            nbrs = sorted(
                (u for u in adj[v] if not visited[u]),
                key=lambda u: (degree[u], u),
            )
            for u in nbrs:
                visited[u] = True
                queue.append(u)
    order.reverse()
    return order


def min_degree_order(matrix):
    """贪心最小度（消去图模型）：每步选择当前度最小的未消去节点。

    消去节点 v 时其剩余邻居两两连边（产生团），这与 LU 填充的图规则一致，
    因此最小度直接最小化每步新增的边数。O(消除度平方) 的朴素实现，
    适合中小规模；超大矩阵请用 RCM。
    """
    n = matrix.n
    adj = matrix.symmetrized_pattern()
    degree = [len(a) for a in adj]
    eliminated = [False] * n
    heap = [(degree[v], v) for v in range(n)]
    heapq.heapify(heap)
    order = []
    for _ in range(n):
        while True:
            d, v = heapq.heappop(heap)
            if not eliminated[v] and d == degree[v]:
                break
        order.append(v)
        eliminated[v] = True
        nbrs = [u for u in adj[v] if not eliminated[u]]
        nbr_set = set(nbrs)
        for u in nbrs:
            au = adj[u]
            au.discard(v)
            au.update(nbr_set)
            au.discard(u)
            degree[u] = len(au)
            heapq.heappush(heap, (degree[u], u))
        adj[v].clear()
        degree[v] = 0
    return order


ORDERS = {
    "natural": lambda a: natural_order(a.n),
    "rcm": rcm_order,
    "mindeg": min_degree_order,
}

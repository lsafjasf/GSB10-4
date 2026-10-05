"""Fill-reducing orderings for sparse Cholesky factorization.

All functions take the off-diagonal adjacency of the graph of A (list of
sets) and return a permutation `perm` such that node `i` of the original
graph is eliminated at position `perm[i]`; i.e. the factorization is done
on B = P A P^T with B[perm[i]][perm[j]] = A[i][j].
"""

import heapq


def identity(n):
    """Natural ordering: eliminate in the order the matrix arrives."""
    return list(range(n))


def rcm(adj):
    """Reverse Cuthill-McKee ordering.

    BFS from a minimum-degree pseudo-peripheral start node, visiting
    neighbours in increasing degree; the final order is reversed.  Cheap
    (O(nnz)) and keeps the profile small -- good for banded/grid graphs.
    """
    n = len(adj)
    degree = [len(a) for a in adj]
    visited = [False] * n
    order = []
    for start in sorted(range(n), key=lambda i: degree[i]):
        if visited[start]:
            continue
        visited[start] = True
        queue = [start]
        head = 0
        while head < len(queue):
            v = queue[head]
            head += 1
            order.append(v)
            nbrs = sorted((u for u in adj[v] if not visited[u]),
                          key=lambda u: degree[u])
            for u in nbrs:
                visited[u] = True
                queue.append(u)
    order.reverse()
    # convert elimination sequence -> position map: perm[node] = step
    perm = [0] * n
    for step, node in enumerate(order):
        perm[node] = step
    return perm


def minimum_degree(adj):
    """(Exact) minimum-degree ordering with lazy priority queue.

    At each step the uneliminated node of smallest current degree is
    eliminated and its uneliminated neighbours are made a clique (this is
    exactly the fill rule applied to the graph).  Sets never contain
    eliminated nodes, so `len(adj[u])` is always the exact current degree.
    Cost is fine for the low-treewidth graphs used here (banded, grids).
    """
    adj = [set(a) for a in adj]
    degree = [len(a) for a in adj]
    heap = [(degree[i], i) for i in range(len(adj))]
    heapq.heapify(heap)
    eliminated = [False] * len(adj)
    order = []
    while heap:
        d, v = heapq.heappop(heap)
        if eliminated[v] or d != degree[v]:
            continue  # stale heap entry
        eliminated[v] = True
        order.append(v)
        nbrs = [u for u in adj[v] if not eliminated[u]]
        adj[v] = set()
        for u in nbrs:
            au = adj[u]
            au.discard(v)
            au.update(w for w in nbrs if w != u)
            degree[u] = len(au)
            heapq.heappush(heap, (degree[u], u))
    # convert elimination sequence -> position map: perm[node] = step
    perm = [0] * len(adj)
    for step, node in enumerate(order):
        perm[node] = step
    return perm


ORDERINGS = {
    "natural": lambda adj: identity(len(adj)),
    "rcm": rcm,
    "mindeg": minimum_degree,
}

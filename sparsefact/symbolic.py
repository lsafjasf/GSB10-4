"""Symbolic Cholesky factorization: counts the fill without doing arithmetic.

Fill rule implemented here (the "fill path theorem", George & Liu 1980):

    L[i][j] != 0  (i > j)  iff  A[i][j] != 0,  or there exists k < j with
    L[i][k] != 0 and L[j][k] != 0.

Equivalently: there is a path i = v0, v1, ..., vm = j in the graph of A
such that every intermediate vertex v1..v(m-1) is eliminated before j
(i.e. has a smaller index in the elimination order).  Eliminating node j
makes its not-yet-eliminated neighbours a clique; every clique edge that
was not already an edge of A is a fill-in edge.

The algorithm below is the standard elimination-tree based one: the row
pattern of column j of L is the union of the upper neighbours of j in A
with the row patterns of the children of j in the elimination tree.
"""

from .errors import MemoryBudgetExceeded


def symbolic_cholesky(adj, fill_cap=None):
    """Count nonzeros of L for the graph `adj` (nodes already in
    elimination order).

    Returns (nnz_l, col_counts) where nnz_l counts the diagonal too.
    Raises MemoryBudgetExceeded as soon as the running count exceeds
    `fill_cap` -- a controlled refusal instead of letting the process
    blow up.  Peak extra memory is only the current "front": each
    column pattern is freed as soon as its parent has absorbed it.
    """
    n = len(adj)
    children = [[] for _ in range(n)]
    pattern = [None] * n
    counts = [0] * n
    nnz = 0
    for j in range(n):
        reach = set(u for u in adj[j] if u > j)
        for c in children[j]:
            reach |= pattern[c]
            pattern[c] = None  # free child's pattern: keeps peak memory low
        reach.discard(j)
        pattern[j] = reach
        counts[j] = len(reach) + 1
        nnz += counts[j]
        if fill_cap is not None and nnz > fill_cap:
            raise MemoryBudgetExceeded(
                "symbolic factorization aborted at column %d: fill-in "
                "exceeded the cap (%s > %s nonzeros in L). Reorder the "
                "matrix (rcm/mindeg) or raise fill_cap." %
                (j, format(nnz, ","), format(fill_cap, ",")))
        if reach:
            children[min(reach)].append(j)
    return nnz, counts


def estimate_flops(col_counts):
    """Rough flop count of numeric Cholesky: sum over columns of the
    rank-1 update cost of its front, ~ (c+1)(c+2)/2 per column."""
    return sum((c + 1) * (c + 2) // 2 for c in col_counts)

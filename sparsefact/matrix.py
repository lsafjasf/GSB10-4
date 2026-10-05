"""Symmetric sparse matrix storage and test-matrix generators.

Storage: full symmetric dict-of-dicts (row i -> {col j: value}), so both
A[i][j] and A[j][i] are stored for an off-diagonal entry.  Only the Python
standard library is used.
"""

import random


class SparseMatrix:
    """Symmetric sparse matrix, n x n, stored as a list of row dicts."""

    def __init__(self, n):
        self.n = n
        self.rows = [dict() for _ in range(n)]

    def add(self, i, j, value):
        """Add `value` to entries (i, j) and (j, i) (symmetric update)."""
        ri = self.rows[i]
        ri[j] = ri.get(j, 0.0) + value
        if i != j:
            rj = self.rows[j]
            rj[i] = rj.get(i, 0.0) + value

    @property
    def nnz(self):
        """Number of stored nonzeros (both triangles counted)."""
        return sum(len(r) for r in self.rows)

    def matvec(self, x):
        """Return A @ x."""
        n = self.n
        y = [0.0] * n
        rows = self.rows
        for i in range(n):
            s = 0.0
            for j, v in rows[i].items():
                s += v * x[j]
            y[i] = s
        return y

    def adjacency(self):
        """Off-diagonal adjacency of the graph of A, as a list of sets."""
        adj = [set() for _ in range(self.n)]
        for i, row in enumerate(self.rows):
            ai = adj[i]
            for j in row:
                if j != i:
                    ai.add(j)
        return adj


def permuted(mat, perm):
    """Return B = P A P^T with B[perm[i]][perm[j]] = A[i][j]."""
    n = mat.n
    out = SparseMatrix(n)
    orows = out.rows
    for i in range(n):
        pi = perm[i]
        di = orows[pi]
        for j, v in mat.rows[i].items():
            di[perm[j]] = v
    return out


def random_permutation(n, seed=0):
    p = list(range(n))
    random.Random(seed).shuffle(p)
    return p


# ---------------------------------------------------------------------------
# Test-matrix generators.  All generators produce symmetric positive definite
# (strictly diagonally dominant) matrices so that Cholesky without pivoting
# is numerically safe.
# ---------------------------------------------------------------------------

def diagonal(n, seed=1):
    """Diagonal matrix with entries in (1, 2)."""
    rng = random.Random(seed)
    m = SparseMatrix(n)
    for i in range(n):
        m.add(i, i, 1.0 + rng.random())
    return m


def banded(n, bandwidth=5, seed=1):
    """Random symmetric banded matrix, half-bandwidth `bandwidth`.

    nnz = n * (1 + 2*bandwidth) - bandwidth*(bandwidth+1).
    """
    rng = random.Random(seed)
    m = SparseMatrix(n)
    for i in range(n):
        for k in range(1, bandwidth + 1):
            j = i + k
            if j < n:
                m.add(i, j, -(0.5 + rng.random()))
    _make_diagonally_dominant(m, rng)
    return m


def random_sparse(n, avg_degree=8, seed=1, margin=1.0):
    """Random symmetric sparsity pattern, ~avg_degree nonzeros per row."""
    rng = random.Random(seed)
    m = SparseMatrix(n)
    half = max(1, avg_degree // 2)
    for i in range(n):
        for _ in range(half):
            j = rng.randrange(n)
            if j != i:
                m.add(i, j, -(0.5 + rng.random()))
    _make_diagonally_dominant(m, rng, margin)
    return m


def near_dense(n, density=0.5, seed=1, margin=1.0):
    """Dense-leaning random symmetric matrix with the given entry density."""
    rng = random.Random(seed)
    m = SparseMatrix(n)
    for i in range(n):
        for j in range(i + 1, n):
            if rng.random() < density:
                m.add(i, j, -(0.5 + rng.random()))
    _make_diagonally_dominant(m, rng, margin)
    return m


def grid9(m_side):
    """9-point stencil on an m_side x m_side 2-D grid (n = m_side**2)."""
    n = m_side * m_side
    m = SparseMatrix(n)

    def node(r, c):
        return r * m_side + c

    for r in range(m_side):
        for c in range(m_side):
            i = node(r, c)
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < m_side and 0 <= cc < m_side:
                        m.add(i, node(rr, cc), -1.0)
    for i in range(n):
        s = sum(abs(v) for j, v in m.rows[i].items() if j != i)
        m.add(i, i, s + 1.0)
    return m


def _make_diagonally_dominant(m, rng, margin=1.0):
    # small `margin` -> weakly dominant -> ill-conditioned (for refinement
    # demos); the default keeps the matrix comfortably SPD.
    for i in range(m.n):
        s = sum(abs(v) for j, v in m.rows[i].items() if j != i)
        m.add(i, i, s + margin * (1.0 + rng.random()))

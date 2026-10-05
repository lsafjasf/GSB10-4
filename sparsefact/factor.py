"""Blocked (panel) right-looking Cholesky factorization A = L L^T with
three layers of memory protection:

1. Pre-flight estimation.  A symbolic factorization counts nnz(L) first;
   if the estimated bytes exceed `memory_budget` and no spill file was
   given, factorize() raises MemoryBudgetExceeded -- a controlled refusal
   instead of an OOM kill.
2. Blocked spilling.  The factor is produced in panels of `block_size`
   columns.  Finished panels are either kept in a compact in-memory list
   or appended to a spill file on disk; the live dict storage of finished
   columns is freed immediately, so the resident set tracks the factor's
   "front", not the whole factor.
3. RSS watchdog.  Every 512 columns the process high-water mark is
   checked against `rss_cap`; exceeding it raises MemoryBudgetExceeded.

Fill is generated dynamically by the rank-1 updates, following exactly
the fill rule documented in symbolic.py.
"""

import math
import pickle
import resource
from array import array

from .errors import MemoryBudgetExceeded
from .ordering import identity
from .symbolic import symbolic_cholesky, estimate_flops

# Heuristic bytes per stored L entry in the numeric phase
# (dict slot + int key + float object), plus per-column overhead below.
BYTES_PER_ENTRY = 96
BYTES_PER_COLUMN = 256

_WATCHDOG_INTERVAL = 512


class Factor:
    """Cholesky factor L of P A P^T, stored as panels of columns.

    Each column is stored as (j, diag, rows, vals) where rows/vals are the
    strictly-lower part of column j as parallel arrays.  Panels live either
    in memory or in a spill file (then only file offsets are kept).
    """

    def __init__(self, n, perm, panels, spill_path, offsets, nnz_l):
        self.n = n
        self.perm = perm
        self._panels = panels          # in-memory panels (or None)
        self._spill_path = spill_path  # spill file path (or None)
        self._offsets = offsets        # byte offset of each spilled panel
        self.nnz_l = nnz_l

    # -- panel iteration ------------------------------------------------
    def _iter_panels(self, reverse=False):
        if self._spill_path is not None:
            offsets = self._offsets[::-1] if reverse else self._offsets
            with open(self._spill_path, "rb") as fh:
                for off in offsets:
                    fh.seek(off)
                    yield pickle.load(fh)
        else:
            yield from (reversed(self._panels) if reverse else self._panels)

    # -- triangular solves (column-oriented, stream over panels) --------
    def solve(self, b):
        """Solve L L^T x = b in the *permuted* space."""
        x = list(b)
        # forward: L x = b, columns in ascending order
        for panel in self._iter_panels():
            for (j, d, rows, vals) in panel:
                xj = x[j] / d
                x[j] = xj
                for r, v in zip(rows, vals):
                    x[r] -= v * xj
        # backward: L^T x = y, columns in descending order
        for panel in self._iter_panels(reverse=True):
            for (j, d, rows, vals) in reversed(panel):
                s = x[j]
                for r, v in zip(rows, vals):
                    s -= v * x[r]
                x[j] = s / d
        return x

    def solve_system(self, b):
        """Solve A x = b in the *original* numbering."""
        perm = self.perm
        pb = [0.0] * self.n
        for i in range(self.n):
            pb[perm[i]] = b[i]
        z = self.solve(pb)
        x = [0.0] * self.n
        for i in range(self.n):
            x[i] = z[perm[i]]
        return x


def factorize(mat, perm=None, block_size=1024, memory_budget=None,
              spill_path=None, fill_cap=50_000_000, rss_cap=None):
    """Factor P A P^T = L L^T.  See module docstring for the memory policy.

    mat           SparseMatrix (symmetric positive definite)
    perm          elimination order (default: natural)
    block_size    columns per panel (the "block" of the blocked algorithm)
    memory_budget bytes; if the symbolic estimate exceeds it and no
                  spill_path is given, refuse with MemoryBudgetExceeded
    spill_path    if given, panels are written to this file during
                  factorization and streamed back during solves
    fill_cap      hard cap on nnz(L) used by the symbolic phase
    rss_cap       hard cap in bytes on the process high-water mark
    """
    n = mat.n
    if perm is None:
        perm = identity(n)

    # ---- phase 0: permuted adjacency (for the symbolic phase) ---------
    adj = [set() for _ in range(n)]
    rows = mat.rows
    for i in range(n):
        pi = perm[i]
        ai = adj[pi]
        for j in rows[i]:
            if j != i:
                ai.add(perm[j])

    # ---- phase 1: symbolic factorization -> fill estimate -------------
    nnz_l, col_counts = symbolic_cholesky(adj, fill_cap=fill_cap)
    del adj
    est_bytes = nnz_l * BYTES_PER_ENTRY + n * BYTES_PER_COLUMN
    if (memory_budget is not None and est_bytes > memory_budget
            and spill_path is None):
        raise MemoryBudgetExceeded(
            "refusing to factorize: estimated factor storage %s bytes "
            "(nnz(L) = %s) exceeds memory budget %s bytes. Enable "
            "spilling (spill_path=...), reorder, or raise the budget." %
            (format(est_bytes, ","), format(nnz_l, ","),
             format(memory_budget, ",")),
            required_bytes=est_bytes, budget_bytes=memory_budget)

    # ---- phase 2: build lower triangle (by columns) of P A P^T --------
    # cols[k] holds rows i >= k, i.e. column k of the lower triangle.
    cols = [dict() for _ in range(n)]
    for i in range(n):
        pi = perm[i]
        for j, v in rows[i].items():
            pj = perm[j]
            if pi <= pj:
                cols[pi][pj] = v
            else:
                cols[pj][pi] = v

    # ---- phase 3: numeric right-looking factorization, panel by panel -
    panels = [] if spill_path is None else None
    offsets = [] if spill_path is not None else None
    fh = open(spill_path, "wb") if spill_path is not None else None
    panel = []

    def flush():
        if not panel:
            return
        if fh is not None:
            offsets.append(fh.tell())
            pickle.dump(panel, fh, protocol=pickle.HIGHEST_PROTOCOL)
        else:
            panels.append(list(panel))
        panel.clear()

    try:
        for j in range(n):
            colj = cols[j]
            d2 = colj[j]
            if d2 <= 0.0:
                raise ValueError(
                    "matrix is not positive definite (pivot %g at column "
                    "%d)" % (d2, j))
            d = math.sqrt(d2)
            below = sorted(i for i in colj if i > j)
            inv = 1.0 / d
            for i in below:
                colj[i] *= inv
            # rank-1 update of the trailing submatrix (creates the fill)
            m = len(below)
            for ai in range(m):
                a = below[ai]
                la = colj[a]
                ca = cols[a]  # entry (b, a), b >= a, lives in column a
                ca[a] -= la * la
                for bi in range(ai + 1, m):
                    b = below[bi]
                    ca[b] = ca.get(b, 0.0) - la * colj[b]
            panel.append((j, d, array("i", below),
                          array("d", (colj[i] for i in below))))
            cols[j] = None  # free live storage of the finished column
            if len(panel) >= block_size:
                flush()
            if rss_cap is not None and (j & (_WATCHDOG_INTERVAL - 1)) == 0:
                rss = resource.getrusage(
                    resource.RUSAGE_SELF).ru_maxrss * 1024
                if rss > rss_cap:
                    raise MemoryBudgetExceeded(
                        "RSS watchdog: process high-water mark %s bytes "
                        "exceeded rss_cap %s bytes at column %d; aborting "
                        "cleanly instead of being OOM-killed." %
                        (format(rss, ","), format(rss_cap, ","), j),
                        required_bytes=rss, budget_bytes=rss_cap)
        flush()
    finally:
        if fh is not None:
            fh.close()

    return Factor(n, perm, panels, spill_path, offsets, nnz_l)

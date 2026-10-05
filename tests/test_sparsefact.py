"""Self-tests for sparsefact.  Run: python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sparsefact import (MemoryBudgetExceeded, banded, diagonal, grid9,
                        iterative_refinement, minimum_degree, near_dense,
                        permuted, random_permutation, random_sparse, rcm,
                        residual_norm, factorize, symbolic_cholesky,
                        estimate_flops)


def make_rhs(mat):
    """Deterministic right-hand side: b = A * x_true, x_true varied."""
    x_true = [((i * 7) % 13) - 6.0 for i in range(mat.n)]
    return mat.matvec(x_true), x_true


class TestSolveCorrectness(unittest.TestCase):
    """Factor + solve must reproduce the known solution for every
    matrix family: diagonal, banded, random sparse, near-dense, grid."""

    def check_case(self, mat, **kw):
        b, x_true = make_rhs(mat)
        f = factorize(mat, **kw)
        x = f.solve_system(b)
        err = max(abs(a - c) for a, c in zip(x, x_true))
        res = residual_norm(mat, x, b)
        self.assertLess(err, 1e-6)
        self.assertLess(res, 1e-9)

    def test_diagonal(self):
        self.check_case(diagonal(500, seed=3))

    def test_banded(self):
        self.check_case(banded(800, bandwidth=4, seed=4))

    def test_random_sparse(self):
        self.check_case(random_sparse(400, avg_degree=6, seed=5))

    def test_near_dense(self):
        self.check_case(near_dense(200, density=0.5, seed=6))

    def test_grid9(self):
        self.check_case(grid9(20))  # n = 400

    def test_permuted_solve(self):
        mat = banded(500, bandwidth=5, seed=7)
        adj = mat.adjacency()
        for order in (rcm(adj), minimum_degree(adj)):
            b, x_true = make_rhs(mat)
            f = factorize(mat, perm=order)
            x = f.solve_system(b)
            err = max(abs(a - c) for a, c in zip(x, x_true))
            self.assertLess(err, 1e-6)


class TestFillRule(unittest.TestCase):
    """The symbolic fill count must exactly match the numeric factor."""

    def test_symbolic_matches_numeric(self):
        for mat in (banded(300, 3, seed=1), random_sparse(200, 5, seed=2),
                    grid9(15)):
            adj = mat.adjacency()
            for name, order in (("natural", list(range(mat.n))),
                                ("rcm", rcm(adj)),
                                ("mindeg", minimum_degree(adj))):
                # symbolic count in the permuted graph
                padj = [set() for _ in range(mat.n)]
                for i in range(mat.n):
                    for j in adj[i]:
                        padj[order[i]].add(order[j])
                nnz_sym, _ = symbolic_cholesky(padj)
                f = factorize(mat, perm=order)
                self.assertEqual(
                    nnz_sym, f.nnz_l,
                    "%s/%s: symbolic %d != numeric %d" %
                    (name, mat.n, nnz_sym, f.nnz_l))


class TestOrdering(unittest.TestCase):
    """RCM and minimum-degree must massively reduce fill on a badly
    numbered banded matrix."""

    def test_orderings_reduce_fill(self):
        mat = permuted(banded(3000, bandwidth=4, seed=8),
                       random_permutation(3000, seed=9))
        adj = mat.adjacency()
        counts = {}
        for name, order in (("natural", list(range(mat.n))),
                            ("rcm", rcm(adj)),
                            ("mindeg", minimum_degree(adj))):
            padj = [set() for _ in range(mat.n)]
            for i in range(mat.n):
                for j in adj[i]:
                    padj[order[i]].add(order[j])
            counts[name], _ = symbolic_cholesky(padj)
        self.assertLess(counts["rcm"], counts["natural"] // 10)
        self.assertLess(counts["mindeg"], counts["natural"] // 10)

    def test_permutation_valid(self):
        adj = random_sparse(300, 5, seed=3).adjacency()
        for order in (rcm(adj), minimum_degree(adj)):
            self.assertEqual(sorted(order), list(range(300)))


class TestMemoryPolicy(unittest.TestCase):
    """Memory overruns must surface as MemoryBudgetExceeded, never as an
    OOM kill."""

    def test_budget_refusal(self):
        mat = banded(2000, bandwidth=5, seed=1)
        with self.assertRaises(MemoryBudgetExceeded) as ctx:
            factorize(mat, memory_budget=1024)  # absurdly small budget
        self.assertIn("refusing to factorize", str(ctx.exception))

    def test_fill_cap_refusal(self):
        # badly numbered banded matrix -> fill explodes -> symbolic aborts
        mat = permuted(banded(4000, bandwidth=4, seed=2),
                       random_permutation(4000, seed=3))
        with self.assertRaises(MemoryBudgetExceeded) as ctx:
            factorize(mat, fill_cap=200_000)
        self.assertIn("symbolic factorization aborted", str(ctx.exception))

    def test_rss_watchdog(self):
        mat = banded(3000, bandwidth=5, seed=4)
        with self.assertRaises(MemoryBudgetExceeded) as ctx:
            factorize(mat, rss_cap=1)  # 1 byte: trips at first check
        self.assertIn("RSS watchdog", str(ctx.exception))

    def test_spill_matches_memory(self):
        mat = banded(1500, bandwidth=4, seed=5)
        b, x_true = make_rhs(mat)
        f_mem = factorize(mat, block_size=64)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "L.spill")
            # tiny budget, but spilling is allowed -> must succeed
            f_spill = factorize(mat, block_size=64, memory_budget=1024,
                                spill_path=path)
            self.assertTrue(os.path.getsize(path) > 0)
            x1 = f_mem.solve_system(b)
            x2 = f_spill.solve_system(b)
        self.assertEqual(f_mem.nnz_l, f_spill.nnz_l)
        err = max(abs(a - c) for a, c in zip(x1, x2))
        self.assertLess(err, 1e-12)
        err_true = max(abs(a - c) for a, c in zip(x2, x_true))
        self.assertLess(err_true, 1e-6)


class TestRefinement(unittest.TestCase):
    def test_residual_history_decreases(self):
        mat = banded(600, bandwidth=5, seed=11)
        b, _ = make_rhs(mat)
        f = factorize(mat)
        _, history = iterative_refinement(mat, f, b)
        self.assertLessEqual(history[-1][0], 1e-12)
        self.assertLessEqual(history[-1][0], history[0][0])


if __name__ == "__main__":
    unittest.main()

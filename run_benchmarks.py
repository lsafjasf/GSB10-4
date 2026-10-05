#!/usr/bin/env python3
"""Benchmark & demo driver for sparsefact.

Usage:
    python3 run_benchmarks.py            # everything
    python3 run_benchmarks.py cases      # fill/ordering/residual case table
    python3 run_benchmarks.py memory     # memory-limit policy demos
    python3 run_benchmarks.py refine     # residual-vs-iteration table
    python3 run_benchmarks.py big        # 100k-order / ~1M-nnz benchmark
"""

import resource
import sys
import tempfile
import time

from sparsefact import (MemoryBudgetExceeded, ORDERINGS, banded, diagonal,
                        estimate_flops, factorize, grid9,
                        iterative_refinement, near_dense, permuted,
                        random_permutation, random_sparse, residual_norm)

FLOP_LIMIT = 60_000_000  # skip numeric factorization above this (pure Python)


def rss_mb():
    """Process high-water mark in MiB (Linux: ru_maxrss is KiB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def permute_adj(adj, perm):
    n = len(adj)
    out = [set() for _ in range(n)]
    for i in range(n):
        oi = out[perm[i]]
        for j in adj[i]:
            oi.add(perm[j])
    return out


def case_table():
    from sparsefact import symbolic_cholesky
    print("=" * 96)
    print("CASE TABLE: fill-in per ordering (nnz before vs after factorization)")
    print("=" * 96)
    cases = [
        ("diagonal   n=5000", diagonal(5000, seed=1)),
        ("banded bw=7 n=5000", banded(5000, bandwidth=7, seed=2)),
        ("random deg=8 n=1500", random_sparse(1500, avg_degree=8, seed=3)),
        ("near-dense 50% n=250", near_dense(250, density=0.5, seed=4)),
        ("grid9 40x40 n=1600", grid9(40)),
    ]
    hdr = ("%-22s %-8s %10s %10s %10s %8s %12s %9s %10s %9s"
           % ("case", "ordering", "nnz(A)", "nnz(L)", "fill-in", "fill x",
              "est.flops", "fact.[s]", "residual", "refine it"))
    print(hdr)
    print("-" * len(hdr))
    for name, mat in cases:
        adj = mat.adjacency()
        b = [((i * 7) % 13) - 6.0 for i in range(mat.n)]
        b = mat.matvec(b)
        for oname, ofn in ORDERINGS.items():
            t0 = time.perf_counter()
            perm = ofn(adj)
            t_ord = time.perf_counter() - t0
            nnz_l, counts = symbolic_cholesky(permute_adj(adj, perm))
            flops = estimate_flops(counts)
            if flops <= FLOP_LIMIT:
                t0 = time.perf_counter()
                f = factorize(mat, perm=perm)
                t_fac = time.perf_counter() - t0
                x, hist = iterative_refinement(mat, f, b)
                res = "%.2e" % hist[0][0]
                rit = str(len(hist) - 1)
                tf = "%9.3f" % t_fac
            else:
                res, rit, tf = "(skip)", "(skip)", "     (skip)"
            nnz_upper = (mat.nnz + mat.n) // 2  # one triangle + diagonal
            print("%-22s %-8s %10d %10d %10d %8.2f %12d %9s %10s %9s"
                  % (name, oname, mat.nnz, nnz_l, nnz_l - nnz_upper,
                     nnz_l / nnz_upper, flops, tf, res, rit))
        print("-" * len(hdr))


def memory_demos():
    print("=" * 96)
    print("MEMORY-LIMIT POLICY: controlled refusal, watchdog, and spill-to-disk")
    print("=" * 96)

    # 1) pre-flight refusal: estimated factor storage > budget, no spill
    mat = banded(5000, bandwidth=5, seed=1)
    try:
        factorize(mat, memory_budget=1_000_000)
        print("1) refusal demo: UNEXPECTEDLY SUCCEEDED")
    except MemoryBudgetExceeded as e:
        print("1) pre-flight refusal (budget 1 MB, no spill):")
        print("   MemoryBudgetExceeded: %s" % e)

    # 2) RSS watchdog abort
    try:
        factorize(mat, rss_cap=1)
        print("2) watchdog demo: UNEXPECTEDLY SUCCEEDED")
    except MemoryBudgetExceeded as e:
        print("2) RSS watchdog (rss_cap=1 byte):")
        print("   MemoryBudgetExceeded: %s" % e)

    # 3) spill-to-disk: same tiny budget, but spilling allowed -> succeeds
    with tempfile.TemporaryDirectory() as td:
        import os
        path = os.path.join(td, "L.spill")
        t0 = time.perf_counter()
        f = factorize(mat, block_size=256, memory_budget=1_000_000,
                      spill_path=path)
        t = time.perf_counter() - t0
        size = os.path.getsize(path)
        x_true = [((i * 7) % 13) - 6.0 for i in range(mat.n)]
        b = mat.matvec(x_true)
        x = f.solve_system(b)
        res = residual_norm(mat, x, b)
        print("3) spill-to-disk (budget 1 MB + spill file): SUCCEEDED")
        print("   panels of 256 columns, spill file %.1f MiB, factor+solve "
              "%.2f s, residual %.2e" % (size / 2**20, t, res))
    print("   -> the process never gets OOM-killed: it either refuses with")
    print("      MemoryBudgetExceeded or completes via blocked spilling.")


def refine_table():
    print("=" * 96)
    print("RESIDUAL vs ITERATION (iterative refinement)")
    print("residual = ||b-Ax||_inf/||b||_inf,  fwd.err = ||x-x_true||_inf/||x_true||_inf")
    print("=" * 96)
    for name, mat in [("banded bw=5 n=2000", banded(2000, 5, seed=7)),
                      ("random deg=6 n=800 (ill-cond, margin=1e-6)",
                       random_sparse(800, 6, seed=8, margin=1e-6)),
                      ("near-dense 60% n=300 (ill-cond, margin=1e-6)",
                       near_dense(300, 0.6, seed=9, margin=1e-6))]:
        f = factorize(mat)
        x_true = [((i * 7) % 13) - 6.0 for i in range(mat.n)]
        b = mat.matvec(x_true)
        _, hist = iterative_refinement(mat, f, b, tol=0.0, max_iter=3,
                                       x_true=x_true)
        print(name)
        for k, (res, fe) in enumerate(hist):
            print("    it%d: residual=%.2e  fwd.err=%.2e" % (k, res, fe))


def big_benchmark():
    print("=" * 96)
    print("BIG BENCHMARK: n = 100,000, banded bw=5 -> nnz(A) ~ 1.1M, matrix")
    print("stored in a *random* order (as badly-numbered input arrives)")
    print("=" * 96)
    n = 100_000
    t0 = time.perf_counter()
    mat = permuted(banded(n, bandwidth=5, seed=42),
                   random_permutation(n, seed=43))
    adj = mat.adjacency()
    print("built matrix: n=%d nnz(A)=%d  (%.1f s, RSS %.0f MiB)"
          % (n, mat.nnz, time.perf_counter() - t0, rss_mb()))

    x_true = [((i * 7) % 13) - 6.0 for i in range(n)]
    b = mat.matvec(x_true)

    nnz_upper = (mat.nnz + n) // 2
    print("one triangle of A: %d nonzeros; fill x is nnz(L)/that" % nnz_upper)
    print("%-8s %12s %10s %8s %8s %10s %10s %10s %12s"
          % ("ordering", "nnz(L)", "fill-in", "fill x", "ord.[s]",
             "fact.[s]", "solve[s]", "peak MiB", "residual"))
    print("-" * 94)
    for oname in ("natural", "rcm", "mindeg"):
        ofn = ORDERINGS[oname]
        t0 = time.perf_counter()
        perm = ofn(adj)
        t_ord = time.perf_counter() - t0
        try:
            t0 = time.perf_counter()
            f = factorize(mat, perm=perm, block_size=2048,
                          fill_cap=5_000_000)
            t_fac = time.perf_counter() - t0
            t0 = time.perf_counter()
            x = f.solve_system(b)
            t_sol = time.perf_counter() - t0
            res = "%.2e" % residual_norm(mat, x, b)
            print("%-8s %12d %10d %8.2f %8.1f %10.1f %10.1f %10.0f %12s"
                  % (oname, f.nnz_l, f.nnz_l - nnz_upper,
                     f.nnz_l / nnz_upper, t_ord, t_fac, t_sol,
                     rss_mb(), res))
        except MemoryBudgetExceeded as e:
            print("%-8s   REFUSED after %.1f s: %s"
                  % (oname, time.perf_counter() - t0, e))
        del perm
    print("RSS baseline after matrix build: %.0f MiB (high-water mark, cumulative)" % rss_mb())


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "cases"):
        case_table()
    if which in ("all", "memory"):
        memory_demos()
    if which in ("all", "refine"):
        refine_table()
    if which in ("all", "big"):
        big_benchmark()

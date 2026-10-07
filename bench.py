"""Scale benchmark: 10,000 variables, 100,000 pairwise edges.

Run:  python3 bench.py
Reports wall time and peak RSS (resource.getrusage) plus tracemalloc peak
for Python-object allocations.
"""

import json
import math
import os
import random
import resource
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pgm import FactorGraph, BeliefPropagation

N_VARS = 10_000
N_EDGES = 100_000
SEED = 20261007


def build_random_graph(n_vars, n_edges, seed):
    rng = random.Random(seed)
    g = FactorGraph()
    for i in range(n_vars):
        g.add_variable(f"x{i}", 2)
    for i in range(n_vars):
        h = rng.uniform(-0.5, 0.5)
        g.add_factor([f"x{i}"], [math.exp(h), math.exp(-h)])
    seen = set()
    while len(seen) < n_edges:
        a = rng.randrange(n_vars)
        b = rng.randrange(n_vars)
        if a == b:
            continue
        edge = (a, b) if a < b else (b, a)
        if edge in seen:
            continue
        seen.add(edge)
        j = rng.uniform(-0.4, 0.4)
        g.add_factor([f"x{a}", f"x{b}"],
                     [math.exp(j), math.exp(-j),
                      math.exp(-j), math.exp(j)])
    return g


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main():
    print(f"building graph: {N_VARS} vars, {N_EDGES} pairwise edges ...")
    t0 = time.perf_counter()
    g = build_random_graph(N_VARS, N_EDGES, SEED)
    t_build = time.perf_counter() - t0
    print(f"build: {t_build:.2f}s, factors={len(g.factors)}, "
          f"peak RSS so far {peak_rss_mb():.1f} MB")

    t0 = time.perf_counter()
    bp = BeliefPropagation(g, damping=0.3, tol=1e-6, max_iter=300)
    res = bp.run()
    t_run = time.perf_counter() - t0
    rss_after_inference = peak_rss_mb()

    # separate short instrumented run for Python-object allocation peak
    # (tracemalloc slows execution ~3x, so it is kept out of the timed run)
    tracemalloc.start()
    BeliefPropagation(g, damping=0.3, tol=1e-6, max_iter=3).run()
    _, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    out = dict(
        variables=N_VARS, pairwise_edges=N_EDGES, factors=len(g.factors),
        damping=0.3, tol=1e-6,
        status=res.status, iterations=res.iterations,
        final_residual=res.residuals[-1],
        build_seconds=round(t_build, 3),
        inference_seconds=round(t_run, 3),
        seconds_per_iteration=round(t_run / max(res.iterations, 1), 3),
        peak_rss_mb=round(rss_after_inference, 1),
        peak_rss_mb_incl_tracemalloc=round(peak_rss_mb(), 1),
        tracemalloc_peak_mb_3iters=round(py_peak / 1024 / 1024, 1),
        python=sys.version.split()[0],
        machine=os.uname().machine,
    )
    print(json.dumps(out, indent=2))
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "results", "benchmark.json"), "w") as fh:
        json.dump(out, fh, indent=2)


if __name__ == "__main__":
    main()

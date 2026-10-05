#!/usr/bin/env python3
"""Benchmark: 100k nodes / 500k edges planted-partition graph.

Run:  python3 benchmark.py
Writes:
  out/benchmark_data.json    timing / memory / reproducibility data
  out/benchmark_report.txt   per-community comparison table
"""

from __future__ import annotations

import json
import os
import platform
import resource
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import graphs
import metrics
from community import detect_communities

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def rss_mb() -> float:
    # ru_maxrss is KiB on Linux, bytes on macOS.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1024.0 if sys.platform != "darwin" else peak / 1024.0**2


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    data = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "config": {
            "n": 100_000, "m": 500_000, "n_clusters": 40,
            "intra_fraction": 0.9, "n_isolated": 100, "n_selfloops": 50,
            "seed": 20261005, "detect_seed": 42, "order": "sorted",
        },
    }

    # ---- build graph -----------------------------------------------------
    t0 = time.perf_counter()
    g, meta = graphs.planted_benchmark()
    t_build = time.perf_counter() - t0
    n_edges = sum(g.degree(u) for u in g.nodes) // 2
    print(f"graph built: {len(g.nodes)} nodes, {n_edges} edges "
          f"(incl. self loops) in {t_build:.2f}s")
    data["build_seconds"] = round(t_build, 3)
    data["edges"] = n_edges

    # ---- detection run 1 (timed + tracemalloc) ---------------------------
    tracemalloc.start()
    t0 = time.perf_counter()
    r1 = detect_communities(g, seed=42, order="sorted")
    t_detect1 = time.perf_counter() - t0
    _, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss1 = rss_mb()
    print(f"run 1: {t_detect1:.2f}s, communities={r1['communities']}, "
          f"Q={r1['modularity']:.4f}, stop={r1['stop_reason']}")

    # ---- detection run 2 (reproducibility cross-check) -------------------
    t0 = time.perf_counter()
    r2 = detect_communities(g, seed=42, order="sorted")
    t_detect2 = time.perf_counter() - t0
    identical = r1["partition"] == r2["partition"]
    ari = metrics.adjusted_rand_index(r1["partition"], r2["partition"])
    print(f"run 2: {t_detect2:.2f}s, identical partition: {identical} "
          f"(ARI={ari:.6f})")

    # ---- different seed, sorted order: must still be identical -----------
    r3 = detect_communities(g, seed=999, order="sorted")
    ari_seed = metrics.adjusted_rand_index(r1["partition"], r3["partition"])
    print(f"seed 999 (sorted order): ARI vs seed 42 = {ari_seed:.6f}")

    # ---- quality vs planted ground truth ---------------------------------
    planted = {i: c for i, c in enumerate(meta["planted_cluster_of"])}
    for i in range(len(planted), len(g.nodes)):
        planted[i] = -1  # isolated nodes: no planted community
    ari_truth = metrics.adjusted_rand_index(r1["partition"], planted)
    print(f"ARI vs planted ground truth: {ari_truth:.4f}")

    gm = metrics.global_metrics(g, r1["partition"])
    rows = metrics.community_report(g, r1["partition"])
    sizes = sorted(rw["size"] for rw in rows)
    singletons = sum(1 for rw in rows if rw["size"] == 1)

    data.update(
        {
            "detect_seconds_run1": round(t_detect1, 3),
            "detect_seconds_run2": round(t_detect2, 3),
            "peak_rss_mb": round(rss1, 1),
            "peak_python_alloc_mb": round(py_peak / 1024**2, 1),
            "communities": r1["communities"],
            "singleton_communities": singletons,
            "modularity": round(r1["modularity"], 6),
            "coverage": round(gm["coverage"], 6),
            "mean_conductance": round(gm["mean_conductance"], 6),
            "levels": r1["levels"],
            "passes_per_level": r1["passes_per_level"],
            "stop_reason": r1["stop_reason"],
            "run1_eq_run2": identical,
            "ari_run1_run2": ari,
            "ari_seed42_vs_seed999": ari_seed,
            "ari_vs_planted_truth": round(ari_truth, 6),
            "community_size_min": sizes[0],
            "community_size_median": sizes[len(sizes) // 2],
            "community_size_max": sizes[-1],
        }
    )

    with open(os.path.join(OUT_DIR, "benchmark_data.json"), "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    with open(os.path.join(OUT_DIR, "benchmark_report.txt"), "w") as f:
        f.write(metrics.format_report(g, r1["partition"]) + "\n")

    print(f"peak RSS {rss1:.1f} MB, python alloc peak "
          f"{py_peak / 1024**2:.1f} MB")
    print(f"data written to {OUT_DIR}/benchmark_data.json")
    return 0 if identical and ari_seed == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

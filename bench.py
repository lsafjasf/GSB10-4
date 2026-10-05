#!/usr/bin/env python3
"""Benchmark: 1,000,000 reads + 1,000 switches. Stdlib only.

Reports wall time and peak memory (ru_maxrss + tracemalloc) for:
  A. 1,000,000 reads against a stable version
  B. 1,000 switches (each commits a 100-key version, durable on disk)
  C. interleaved: 1,000,000 reads with a switch every 1,000 reads

Writes results/bench.json.
"""
import json
import os
import resource
import shutil
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from versioned_store import VersionedStore

HERE = os.path.dirname(os.path.abspath(__file__))
N_READS = 1_000_000
N_SWITCHES = 1_000
N_KEYS = 100


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def phase_reads(store):
    tracemalloc.start()
    t0 = time.perf_counter()
    total = 0
    for i in range(N_READS):
        total += len(store.get("k%03d" % (i % N_KEYS), ""))
    dt = time.perf_counter() - t0
    _, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert total > 0
    return {"reads": N_READS, "seconds": round(dt, 3),
            "reads_per_sec": round(N_READS / dt),
            "python_alloc_peak_mb": round(py_peak / 1e6, 2),
            "process_peak_rss_mb": round(peak_rss_mb(), 1)}


def phase_switches(store):
    tracemalloc.start()
    t0 = time.perf_counter()
    for n in range(N_SWITCHES):
        store.commit_with_builder(
            lambda v: {"k%03d" % i: "v%06d" % v for i in range(N_KEYS)})
    dt = time.perf_counter() - t0
    _, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"switches": N_SWITCHES, "seconds": round(dt, 3),
            "switches_per_sec": round(N_SWITCHES / dt, 1),
            "python_alloc_peak_mb": round(py_peak / 1e6, 2),
            "process_peak_rss_mb": round(peak_rss_mb(), 1)}


def phase_interleaved(store):
    tracemalloc.start()
    t0 = time.perf_counter()
    reads_done = switches_done = 0
    for round_no in range(N_SWITCHES):
        for i in range(N_READS // N_SWITCHES):
            store.get("k%03d" % (i % N_KEYS), "")
            reads_done += 1
        store.commit_with_builder(
            lambda v: {"k%03d" % i: "v%06d" % v for i in range(N_KEYS)})
        switches_done += 1
    dt = time.perf_counter() - t0
    _, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"reads": reads_done, "switches": switches_done,
            "seconds": round(dt, 3),
            "python_alloc_peak_mb": round(py_peak / 1e6, 2),
            "process_peak_rss_mb": round(peak_rss_mb(), 1)}


def main():
    d = tempfile.mkdtemp(prefix="vs-bench-")
    try:
        store = VersionedStore(os.path.join(d, "s"))
        store.commit_with_builder(
            lambda v: {"k%03d" % i: "v%06d" % v for i in range(N_KEYS)})
        results = {
            "config": {"reads": N_READS, "switches": N_SWITCHES,
                       "keys_per_version": N_KEYS,
                       "python": sys.version.split()[0]},
            "A_pure_reads_1M": phase_reads(store),
            "B_pure_switches_1000": phase_switches(store),
            "C_interleaved_1M_reads_1000_switches": phase_interleaved(store),
        }
        results["final_version"] = store.version()
        out = os.path.join(HERE, "results", "bench.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            json.dump(results, f, indent=2)
        print(json.dumps(results, indent=2))
    finally:
        shutil.rmtree(d, True)


if __name__ == "__main__":
    main()

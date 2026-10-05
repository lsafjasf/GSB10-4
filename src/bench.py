"""Benchmark: 10,000 states / 50,000 transitions.

Measures wall time of each analysis phase and peak memory
(Python allocator peak via tracemalloc + process RSS via getrusage).

Run:  python3 src/bench.py
"""
import gc
import json
import resource
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fsm_observability import (
    analyze, equivalence_partition, missing_transitions,
    random_machine, reachable_states, unreachable_states, verify_partition,
)

N_STATES = 10_000
N_TRANS = 50_000
N_INPUTS = 8


def timed(fn, *args):
    t0 = time.perf_counter()
    res = fn(*args)
    return res, time.perf_counter() - t0


def run_once(seed):
    gc.collect()
    tracemalloc.start()
    t_total0 = time.perf_counter()
    machine, t_build = timed(random_machine, N_STATES, N_TRANS, N_INPUTS, seed)
    reach, t_reach = timed(reachable_states, machine)
    unr, t_unr = timed(unreachable_states, machine)
    miss, t_miss = timed(missing_transitions, machine)
    (classes, color), t_part = timed(equivalence_partition, machine,
                                     sorted(reach, key=repr))
    (ok, viol), t_verify = timed(verify_partition, machine, classes)
    t_total = time.perf_counter() - t_total0
    cur, peak_py = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KiB Linux
    return {
        "seed": seed,
        "states": len(machine.states),
        "transitions": len(machine.delta),
        "reachable": len(reach),
        "unreachable": len(unr),
        "missing_transitions": len(miss),
        "equivalence_classes": len(classes),
        "multi_state_classes": sum(1 for c in classes if len(c) > 1),
        "certificate_valid": ok,
        "times_sec": {
            "build_random_machine": round(t_build, 3),
            "reachability": round(t_reach, 3),
            "unreachable": round(t_unr, 3),
            "missing_scan": round(t_miss, 3),
            "partition_refinement": round(t_part, 3),
            "certificate_verify": round(t_verify, 3),
            "total": round(t_total, 3),
        },
        "peak_memory": {
            "tracemalloc_bytes": peak_py,
            "tracemalloc_mib": round(peak_py / 1024 / 1024, 2),
            "maxrss_kib": peak_rss,
            "maxrss_mib": round(peak_rss / 1024, 1),
        },
    }


def main():
    results = [run_once(seed) for seed in (1, 2, 3)]
    best = min(results, key=lambda r: r["times_sec"]["total"])
    report = {"spec": {"states": N_STATES, "transitions": N_TRANS,
                       "inputs": N_INPUTS},
              "runs": results, "best_run_seed": best["seed"]}
    out = Path(__file__).resolve().parent.parent / "data" / "bench_data.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    for r in results:
        print(json.dumps(r, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()

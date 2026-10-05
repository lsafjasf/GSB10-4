"""Benchmark: graceful shutdown with 100k in-flight tasks.

Measures, per variant and across repeated trials:
  * shutdown duration distribution (virtual time, p50/p95/max)
  * per-phase durations
  * wall-clock cost of the simulation itself
  * memory peak (tracemalloc) and process RSS peak (ru_maxrss)

Also cross-checks one shutdown run against the full-run baseline.

Run:  python3 bench.py            # writes results/bench_100k.json
"""
from __future__ import annotations

import json
import os
import resource
import statistics
import tracemalloc

from graceful_shutdown import (
    ShutdownConfig,
    SimEngine,
    make_specs,
    reconcile,
    run_full,
    run_shutdown,
)

N_TASKS = 100_000
WORKERS = 64
CLEANUP_WORKERS = 64
WARMUP = 0.002  # virtual seconds of normal operation before the signal

VARIANTS = {
    "A-comfortable": dict(
        config=ShutdownConfig(drain_timeout=0.1, cleanup_timeout=2.0,
                              force_wait=0.5),
        stuck=0, trials=20,
    ),
    "B-tight": dict(
        config=ShutdownConfig(drain_timeout=0.05, cleanup_timeout=0.3,
                              force_wait=0.1),
        stuck=0, trials=20,
    ),
    "C-tight+stuck": dict(
        config=ShutdownConfig(drain_timeout=0.05, cleanup_timeout=0.3,
                              force_wait=0.1),
        stuck=50, trials=10,
    ),
}
MEM_TRIALS_PER_VARIANT = 3


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def run_trial(seed: int, cfg: ShutdownConfig, stuck: int, measure_mem: bool):
    stuck_ids = frozenset(f"t{i:06d}" for i in range(stuck))
    if measure_mem:
        tracemalloc.start()  # include task specs + engine + shutdown bookkeeping
    specs = make_specs(N_TASKS, seed=seed, stuck_ids=stuck_ids)
    engine = SimEngine(workers=WORKERS, cleanup_workers=CLEANUP_WORKERS)
    for spec in specs:
        engine.submit(spec)
    engine.advance_until(WARMUP)
    inflight = len(engine.unfinished())
    report = run_shutdown(engine, cfg)
    peak = 0
    if measure_mem:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    row = {
        "seed": seed,
        "inflight_at_shutdown": inflight,
        "duration": report.duration,
        "phases": {p.name: p.duration for p in report.phases},
        "counts": report.counts,
        "abandoned": len(report.abandoned),
        "punctual": report.punctual,
        "wall_ms": report.wall_ms,
        "events": engine.metrics()["events_processed"],
        "mem_peak_bytes": peak,
    }
    return engine, report, row


def summarize(name: str, rows: list[dict]) -> dict:
    durations = [r["duration"] for r in rows]
    walls = [r["wall_ms"] for r in rows]
    abandoned = [r["abandoned"] for r in rows]
    mem = [r["mem_peak_bytes"] for r in rows if r["mem_peak_bytes"]]
    summary = {
        "variant": name,
        "trials": len(rows),
        "inflight_mean": statistics.mean(r["inflight_at_shutdown"] for r in rows),
        "duration_p50": pct(durations, 0.50),
        "duration_p95": pct(durations, 0.95),
        "duration_max": max(durations),
        "duration_min": min(durations),
        "wall_ms_p50": pct(walls, 0.50),
        "wall_ms_max": max(walls),
        "abandoned_min": min(abandoned),
        "abandoned_max": max(abandoned),
        "punctual_all": all(r["punctual"] for r in rows),
        "mem_peak_mb_max": (max(mem) / 1e6) if mem else None,
        "phase_mean": {
            phase: statistics.mean(r["phases"][phase] for r in rows)
            for phase in rows[0]["phases"]
        },
        "counts_mean": {
            status: statistics.mean(r["counts"].get(status, 0) for r in rows)
            for status in ("finished", "checkpointed", "rolled_back",
                           "failed", "abandoned")
        },
    }
    return summary


def main() -> None:
    os.makedirs("results", exist_ok=True)
    out = {
        "n_tasks": N_TASKS,
        "workers": WORKERS,
        "cleanup_workers": CLEANUP_WORKERS,
        "warmup_s": WARMUP,
        "variants": {},
    }

    for name, variant in VARIANTS.items():
        rows = []
        first_engine = None
        first_specs_seed = 1000
        for i in range(variant["trials"]):
            seed = first_specs_seed + i
            measure_mem = i < MEM_TRIALS_PER_VARIANT
            engine, report, row = run_trial(
                seed, variant["config"], variant["stuck"], measure_mem)
            if i == 0:
                first_engine = (engine, seed)
            rows.append(row)
            print(f"[{name}] trial {i}: duration={row['duration']:.4f}s "
                  f"abandoned={row['abandoned']} wall={row['wall_ms']:.0f}ms")
        summary = summarize(name, rows)
        out["variants"][name] = {"summary": summary, "trials": rows}

        # Cross-check the first trial of variant A against the full run.
        if name == "A-comfortable":
            engine, seed = first_engine
            specs = make_specs(N_TASKS, seed=seed)
            baseline = run_full(specs, workers=WORKERS)
            recon = reconcile(baseline.results, engine.results,
                              store=engine.store)
            out["reconcile_A_trial0"] = {
                "ok": recon.ok,
                "categories": recon.categories(),
                "violations": recon.violations[:10],
            }
            print(f"[{name}] reconcile vs full run: ok={recon.ok} "
                  f"{recon.categories()}")

    out["rss_peak_mb"] = resource.getrusage(
        resource.RUSAGE_SELF).ru_maxrss / 1024.0

    path = os.path.join("results", "bench_100k.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=2)

    print("\n=== summary ===")
    for name, data in out["variants"].items():
        s = data["summary"]
        print(
            f"{name}: trials={s['trials']} inflight~{s['inflight_mean']:.0f} "
            f"duration p50={s['duration_p50']:.4f}s p95={s['duration_p95']:.4f}s "
            f"max={s['duration_max']:.4f}s | wall p50={s['wall_ms_p50']:.0f}ms "
            f"max={s['wall_ms_max']:.0f}ms | abandoned "
            f"{s['abandoned_min']}..{s['abandoned_max']} | "
            f"punctual_all={s['punctual_all']} | "
            f"mem_peak={s['mem_peak_mb_max'] and round(s['mem_peak_mb_max'], 1)}MB"
        )
    print(f"process RSS peak: {out['rss_peak_mb']:.1f} MB")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()

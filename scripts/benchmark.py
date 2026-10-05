"""Benchmark the conflict reporter on 10,000 constraints / 1,000 variables.

Run from the repository root:

    python3 scripts/benchmark.py            # parent: spawns measured children
    python3 scripts/benchmark.py --child satisfiable|unsatisfiable

Each scenario runs in a fresh child process so that ``resource.ru_maxrss``
reflects that scenario's true peak RSS.  ``tracemalloc`` additionally
captures the Python-side peak allocated by generation / solving /
verification separately.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import resource
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from constraint_conflicts import DifferenceConstraint, analyze  # noqa: E402
from constraint_conflicts.difference_logic import (  # noqa: E402
    DifferenceSystem,
    enumerate_minimal_conflicts,
)
from constraint_conflicts.report import verify_assignment, verify_minimality  # noqa: E402

VARIABLES = 1000
CONSTRAINTS = 10000
SEED = 20261005
CYCLE_LEN = 100
INNER_VARS = frozenset(f"v{i}" for i in range(CYCLE_LEN))


def generate_satisfiable() -> list:
    """DAG (no cycle at all) with mixed positive/negative weights."""
    rng = random.Random(SEED)
    constraints = []
    used = set()
    for i in range(VARIABLES - 1):
        constraints.append(
            DifferenceConstraint(f"s-chain-{i}", f"v{i+1}", f"v{i}", rng.randint(-50, 100))
        )
        used.add((i, i + 1))
    index = len(constraints)
    while len(constraints) < CONSTRAINTS:
        upper = rng.randrange(VARIABLES - 1)
        lower = rng.randrange(upper + 1, VARIABLES)
        if (upper, lower) in used:
            continue
        used.add((upper, lower))
        constraints.append(
            DifferenceConstraint(f"s-rand-{index}", f"v{lower}", f"v{upper}", rng.randint(-50, 100))
        )
        index += 1
    return constraints


def generate_unsatisfiable() -> tuple:
    """Exactly one planted negative cycle of length CYCLE_LEN plus filler."""
    rng = random.Random(SEED + 1)
    constraints = []
    used = set()
    planted_ids = []
    for i in range(CYCLE_LEN):
        upper = f"v{i}"
        lower = f"v{(i + 1) % CYCLE_LEN}"
        bound = -CYCLE_LEN if i == CYCLE_LEN - 1 else 1
        cid = f"planted-{i}"
        planted_ids.append(cid)
        constraints.append(DifferenceConstraint(cid, lower, upper, bound))
        used.add((i, (i + 1) % CYCLE_LEN))

    index = len(constraints)
    while len(constraints) < CONSTRAINTS:
        upper = rng.randrange(VARIABLES - 1)
        lower = rng.randrange(upper + 1, VARIABLES)
        if (upper, lower) in used:
            continue
        used.add((upper, lower))
        # >= CYCLE_LEN inside the cycle's vertex range keeps the planted
        # cycle the unique negative cycle; elsewhere any sign is fine.
        bound = rng.randint(CYCLE_LEN, 200) if lower < CYCLE_LEN else rng.randint(-50, 100)
        constraints.append(
            DifferenceConstraint(f"u-rand-{index}", f"v{lower}", f"v{upper}", bound)
        )
        index += 1
    return constraints, planted_ids


def mib(num_bytes: float) -> float:
    return round(num_bytes / (1024 * 1024), 3)


def run_scenario(name: str) -> dict:
    tracemalloc.start()
    t0 = time.perf_counter()
    if name == "satisfiable":
        constraints = generate_satisfiable()
        planted_ids = None
    else:
        constraints, planted_ids = generate_unsatisfiable()
    generation_s = time.perf_counter() - t0
    _cur, generation_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    system = DifferenceSystem(constraints)

    tracemalloc.start()
    t0 = time.perf_counter()
    report = analyze(constraints, max_conflicts=1, oracle_budget=1000)
    solve_s = time.perf_counter() - t0
    _cur, solve_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    result = {
        "scenario": name,
        "variables": system.variable_count,
        "constraints": system.constraint_count,
        "status": report.status,
        "oracle_calls": report.oracle_calls,
        "truncated": report.truncated,
        "generation_s": round(generation_s, 6),
        "solve_s": round(solve_s, 6),
        "generation_tracemalloc_peak_mib": mib(generation_peak),
        "solve_tracemalloc_peak_mib": mib(solve_peak),
        "process_peak_rss_mib": mib(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
    }

    tracemalloc.start()
    t0 = time.perf_counter()
    if report.status == "no_conflict":
        assert verify_assignment(system, report.witness)
        result["verification"] = {"witness_checked_constraints": system.constraint_count}
    else:
        conflict = report.conflicts[0]
        verification = verify_minimality(system, conflict)
        assert verification.is_minimal

        # Every directed cycle uses the unique index-decreasing edge
        # v99 -> v0, so all negative cycles live in the v0..v99 subgraph.
        # Enumerate that subgraph exhaustively to prove uniqueness.
        inner = DifferenceSystem(
            c
            for c in constraints
            if c.lower in INNER_VARS and c.upper in INNER_VARS
        )
        all_conflicts, truncated, _calls = enumerate_minimal_conflicts(
            inner, max_conflicts=100, oracle_budget=100000
        )
        assert not truncated and len(all_conflicts) == 1
        result["unique_negative_cycle_proven"] = True
        result["conflict"] = {
            "size": conflict.size,
            "total_bound": conflict.total_bound,
            "constraint_ids": list(conflict.constraint_ids),
            "matches_planted_cycle": (
                planted_ids is not None
                and set(conflict.constraint_ids) == set(planted_ids)
            ),
            "removal_checks": [
                {
                    "removed": c.removed_constraint_id,
                    "remaining_satisfiable": c.remaining_is_satisfiable,
                }
                for c in verification.removal_checks
            ],
        }
    result["verification_s"] = round(time.perf_counter() - t0, 6)
    _cur, verify_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result["verification_tracemalloc_peak_mib"] = mib(verify_peak)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=["satisfiable", "unsatisfiable"])
    parser.add_argument("--out", default=str(ROOT / "data" / "benchmark_results.json"))
    args = parser.parse_args()

    if args.child:
        print(json.dumps(run_scenario(args.child)))
        return

    env = dict(os.environ)
    results = []
    for scenario in ("satisfiable", "unsatisfiable"):
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child", scenario],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        results.append(json.loads(completed.stdout))

    payload = {
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor() or "unknown",
            "cpu_count": os.cpu_count(),
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "parameters": {
            "variables": VARIABLES,
            "constraints": CONSTRAINTS,
            "seed": SEED,
            "planted_cycle_length": CYCLE_LEN,
            "max_conflicts": 1,
        },
        "scenarios": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\nwrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()

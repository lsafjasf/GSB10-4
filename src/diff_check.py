"""Differential testing (对拍): fast partition refinement vs the naive
pairwise probing reference on random machines of varied shape.

Writes data/diff_data.json with per-machine stats and a PASS/FAIL summary.

Run:  python3 src/diff_check.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fsm_observability import (
    equivalence_partition, random_machine, reachable_states,
    reference_partition, missing_transitions, unreachable_states,
)


def norm(classes):
    return sorted(frozenset(c) for c in classes)


def main():
    rows = []
    mismatches = 0
    seed = 1
    shapes = [(n, k) for n in (3, 5, 8, 12, 20) for k in (2, 3, 4)]
    reps = 25
    for n, k in shapes:
        for density in (0.25, 0.5, 1.0):
            for _ in range(reps // (3 if n <= 12 else 3)):
                m_edges = max(1, int(n * k * density))
                machine = random_machine(n, m_edges, k, seed=seed)
                reach = len(reachable_states(machine))
                unr = len(unreachable_states(machine))
                miss = len(missing_transitions(machine))
                fast, _ = equivalence_partition(machine)
                slow = reference_partition(machine)
                match = norm(fast) == norm(slow)
                if not match:
                    mismatches += 1
                rows.append({
                    "seed": seed, "states": n, "inputs": k,
                    "transitions": m_edges, "reachable": reach,
                    "unreachable": unr, "missing": miss,
                    "classes_fast": len(fast), "classes_ref": len(slow),
                    "match": match,
                })
                seed += 1

    out_dir = Path(__file__).resolve().parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    summary = {
        "machines_tested": len(rows),
        "mismatches": mismatches,
        "result": "PASS" if mismatches == 0 else "FAIL",
        "cases_with_unreachable": sum(1 for r in rows if r["unreachable"]),
        "cases_with_missing": sum(1 for r in rows if r["missing"]),
        "cases_with_equivalence_classes":
            sum(1 for r in rows if r["classes_fast"] < r["reachable"]),
        "cases_all_singletons":
            sum(1 for r in rows if r["classes_fast"] == r["reachable"]),
    }
    (out_dir / "diff_data.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=1,
                   ensure_ascii=False))
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()

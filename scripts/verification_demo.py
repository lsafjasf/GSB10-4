"""Emit machine-readable minimality verification data for the four cases.

Output: data/minimality_verification.json
- no_conflict: witness assignment independently checked against every rule
- single MUS: every one-constraint deletion has a satisfying assignment
- multiple MUSs (disjoint + overlapping): cross-checked against an
  exhaustive brute-force reference MUS enumerator
- self contradiction: size-one MUS
"""

from __future__ import annotations

import json
import sys
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from constraint_conflicts import DifferenceConstraint, analyze  # noqa: E402
from constraint_conflicts.difference_logic import DifferenceSystem, find_negative_cycle  # noqa: E402
from constraint_conflicts.report import verify_assignment, verify_minimality  # noqa: E402


def brute_force_mus(constraints):
    minimal = []
    for size in range(1, len(constraints) + 1):
        for comb in combinations(range(len(constraints)), size):
            subset = [constraints[i] for i in comb]
            if find_negative_cycle(DifferenceSystem(subset)) is not None:
                if not any(set(m) <= set(comb) for m in minimal):
                    minimal.append(frozenset(comb))
    return sorted(sorted({constraints[i].id for i in m}) for m in minimal)


def summarize_case(name, constraints, **kwargs):
    system = DifferenceSystem(constraints)
    report = analyze(constraints, **kwargs)
    entry = {
        "case": name,
        "constraint_count": system.constraint_count,
        "variable_count": system.variable_count,
        "status": report.status,
        "truncated": report.truncated,
    }
    if report.status == "no_conflict":
        entry["witness"] = report.witness
        entry["witness_satisfies_every_constraint"] = verify_assignment(
            system, report.witness
        )
        return entry

    entry["conflicts"] = []
    for conflict in report.conflicts:
        verification = verify_minimality(system, conflict)
        entry["conflicts"].append(
            {
                "constraint_ids": list(conflict.constraint_ids),
                "size": conflict.size,
                "total_bound": conflict.total_bound,
                "kind": conflict.kind,
                "is_negative_cycle": verification.is_negative_cycle,
                "is_simple_directed_cycle": verification.is_simple_directed_cycle,
                "every_single_removal_satisfiable": (
                    verification.every_single_removal_is_satisfiable
                ),
                "is_minimal": verification.is_minimal,
                "removal_checks": [
                    {
                        "removed_constraint": c.removed_constraint_id,
                        "remaining_satisfiable": c.remaining_is_satisfiable,
                        "witness": c.witness,
                    }
                    for c in verification.removal_checks
                ],
            }
        )
    return entry


CASES = {
    "no_conflict": [
        DifferenceConstraint("c1", "x", "y", 5),
        DifferenceConstraint("c2", "y", "z", 3),
        DifferenceConstraint("c3", "z", "w", -2),
    ],
    "single_minimal_conflict": [
        DifferenceConstraint("a", "x", "y", 1),
        DifferenceConstraint("b", "y", "z", 1),
        DifferenceConstraint("c", "z", "x", -3),
    ],
    "multiple_disjoint_conflicts": [
        DifferenceConstraint("c1", "x", "y", 1),
        DifferenceConstraint("c2", "y", "x", -2),
        DifferenceConstraint("c3", "p", "q", 1),
        DifferenceConstraint("c4", "q", "p", -2),
        DifferenceConstraint("c5", "x", "p", 5),
    ],
    "multiple_overlapping_conflicts": [
        DifferenceConstraint("e1", "x", "y", 1),
        DifferenceConstraint("e2", "y", "x", -2),
        DifferenceConstraint("e3", "z", "x", -3),
        DifferenceConstraint("e4", "y", "z", 1),
    ],
    "self_contradictory_constraint": [
        DifferenceConstraint("bad", "x", "x", -1),
        DifferenceConstraint("ok", "x", "y", 2),
    ],
}


def main():
    entries = []
    for name, constraints in CASES.items():
        entry = summarize_case(
            name,
            constraints,
            max_conflicts=10,
            oracle_budget=1000,
        )
        if entry["status"] == "unsatisfiable":
            reference = brute_force_mus(constraints)
            reported = sorted(sorted(c["constraint_ids"]) for c in entry["conflicts"])
            entry["brute_force_reference_mus"] = reference
            entry["matches_brute_force_reference"] = reported == reference
            entry["cardinality_minimal_size"] = min(c["size"] for c in entry["conflicts"])
        entries.append(entry)

    payload = {
        "definition": (
            "MUS = a constraint set S where S itself is unsatisfiable, but "
            "S\\{c} is satisfiable for every c in S (inclusion-minimal)."
        ),
        "cases": entries,
    }
    out = ROOT / "data" / "minimality_verification.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\nwrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()

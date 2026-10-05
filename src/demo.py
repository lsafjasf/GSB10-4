"""Demo: the four required scenarios and printed evidence.

Run:  python3 src/demo.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fsm_observability import (
    Machine, analyze, distinguishing_sequence, equivalence_evidence,
    exhaustive_pair_check,
)


def show(title, machine):
    print("=" * 72)
    print(title)
    print("=" * 72)
    rep = analyze(machine)
    print("initial state        : %s" % rep["initial"])
    print("states / transitions : %d / %d" % (rep["n_states"], rep["n_transitions"]))
    print("unreachable states   : %s"
          % (rep["unreachable_states"] or "(none)"))
    miss = rep["missing_transitions"]
    print("missing transitions  : %d %s"
          % (len(miss), miss[:8] if miss else "(none)"))
    print("equivalence classes  :")
    for i, c in enumerate(rep["equivalence_classes"]):
        mark = "  <- equivalent" if len(c) > 1 else ""
        print("  C%d = %s%s" % (i, c, mark))
    print("certificate valid    : %s" % rep["certificate_valid"])

    # 1) per-pair evidence for equivalent states
    shown = 0
    for c in rep["equivalence_classes"]:
        if len(c) < 2 or shown >= 3:
            continue
        shown += 1
        p, q = c[0], c[1]
        ev = equivalence_evidence(machine, rep["color"], p, q)
        ex = exhaustive_pair_check(machine, p, q, max_depth=len(machine.states))
        print("  evidence pair %s ~ %s (all %d input symbols agree):"
              % (p, q, len(ev["rows"])))
        for r in ev["rows"]:
            print("    input=%s: outputs %r/%r, successors %s(C%s)/%s(C%s)"
                  % (r["input"], r["p_output"], r["q_output"],
                     r["p_next"], r["p_next_class"],
                     r["q_next"], r["q_next_class"]))
        print("    exhaustive search: %d input sequences tried up to length %d, "
              "counterexample=%s"
              % (ex["sequences_checked"], ex["max_depth"],
                 ex["counterexample_found"]))
        print("    product-automaton distinguishing_sequence = %r"
              % distinguishing_sequence(machine, p, q))

    # 2) a distinguishing sequence for states chosen from different classes
    if len(rep["equivalence_classes"]) >= 2:
        p = rep["equivalence_classes"][0][0]
        q = rep["equivalence_classes"][1][0]
        print("  distinguishing_sequence(%s, %s) = %r"
              % (p, q, distinguishing_sequence(machine, p, q)))
    print()


def case_all_distinguishable():
    # 3 states, 2 inputs, every state pair is distinguishable.
    delta = {
        ("A", "x"): ("B", "0"), ("A", "y"): ("C", "1"),
        ("B", "x"): ("A", "1"), ("B", "y"): ("C", "0"),
        ("C", "x"): ("A", "0"), ("C", "y"): ("B", "0"),
    }
    return Machine(["A", "B", "C"], ["x", "y"], delta, initial="A")


def case_with_equivalent_classes():
    # D and E are exact behavioural duplicates of B (merge targets).
    delta = {
        ("A", "x"): ("B", "0"), ("A", "y"): ("D", "1"),
        ("B", "x"): ("A", "1"), ("B", "y"): ("C", "0"),
        ("C", "x"): ("A", "0"), ("C", "y"): ("B", "0"),
        ("D", "x"): ("E", "1"), ("D", "y"): ("C", "0"),
        ("E", "x"): ("D", "1"), ("E", "y"): ("C", "0"),
    }
    return Machine(["A", "B", "C", "D", "E"], ["x", "y"], delta, initial="A")


def case_with_unreachable():
    # G and H are unreachable from A; they must NOT appear in classes.
    delta = {
        ("A", "x"): ("B", "0"), ("A", "y"): ("B", "1"),
        ("B", "x"): ("A", "1"), ("B", "y"): ("A", "0"),
        ("G", "x"): ("H", "0"), ("G", "y"): ("G", "1"),
        ("H", "x"): ("G", "0"), ("H", "y"): ("H", "1"),
    }
    return Machine(["A", "B", "G", "H"], ["x", "y"], delta, initial="A")


def case_with_missing_transitions():
    # Partial machine: C has no transition on 'y'. B vs C must be
    # distinguishable (silence vs reaction), while D duplicates C.
    delta = {
        ("A", "x"): ("B", "0"), ("A", "y"): ("C", "1"),
        ("B", "x"): ("A", "1"), ("B", "y"): ("D", "0"),
        ("C", "x"): ("A", "0"),
        ("D", "x"): ("A", "0"),
    }
    return Machine(["A", "B", "C", "D"], ["x", "y"], delta, initial="A")


def main():
    show("CASE 1: all states distinguishable", case_all_distinguishable())
    show("CASE 2: equivalence classes exist", case_with_equivalent_classes())
    show("CASE 3: unreachable states present", case_with_unreachable())
    show("CASE 4: missing transitions (partial machine)",
         case_with_missing_transitions())


if __name__ == "__main__":
    main()

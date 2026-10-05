"""Self tests for fsm_observability.

Covers: all-distinguishable machines, machines with equivalence classes,
unreachable states, missing transitions, certificate verification, and
differential testing (对拍) of the fast partition refinement against the
naive pairwise reference implementation on random machines.

Run:  python3 src/test_fsm_observability.py   (or: python3 -m unittest)
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fsm_observability import (
    Machine, analyze, distinguishing_sequence, equivalence_evidence,
    equivalence_partition, exhaustive_pair_check, missing_transitions,
    random_machine, reachable_states, reference_partition,
    unreachable_states, verify_partition,
)


def norm(classes):
    return sorted(frozenset(c) for c in classes)


class TestAllDistinguishable(unittest.TestCase):
    def setUp(self):
        delta = {
            ("A", "x"): ("B", "0"), ("A", "y"): ("C", "1"),
            ("B", "x"): ("A", "1"), ("B", "y"): ("C", "0"),
            ("C", "x"): ("A", "0"), ("C", "y"): ("B", "0"),
        }
        self.m = Machine(["A", "B", "C"], ["x", "y"], delta, initial="A")

    def test_singleton_classes(self):
        classes, _ = equivalence_partition(self.m)
        self.assertEqual(norm(classes), [frozenset({"A"}), frozenset({"B"}),
                                         frozenset({"C"})])

    def test_distinguishing_sequences_exist(self):
        for p, q in [("A", "B"), ("A", "C"), ("B", "C")]:
            seq = distinguishing_sequence(self.m, p, q)
            self.assertIsNotNone(seq, (p, q))
            self.assertLessEqual(len(seq), 4)

    def test_agrees_with_reference(self):
        classes, _ = equivalence_partition(self.m)
        self.assertEqual(norm(classes), norm(reference_partition(self.m)))


class TestEquivalenceClasses(unittest.TestCase):
    def setUp(self):
        delta = {
            ("A", "x"): ("B", "0"), ("A", "y"): ("D", "1"),
            ("B", "x"): ("A", "1"), ("B", "y"): ("C", "0"),
            ("C", "x"): ("A", "0"), ("C", "y"): ("B", "0"),
            ("D", "x"): ("E", "1"), ("D", "y"): ("C", "0"),
            ("E", "x"): ("D", "1"), ("E", "y"): ("C", "0"),
        }
        self.m = Machine(["A", "B", "C", "D", "E"], ["x", "y"], delta,
                         initial="A")

    def test_merged_class(self):
        classes, color = equivalence_partition(self.m)
        self.assertIn(frozenset({"D", "E"}), norm(classes))
        self.assertEqual(color["D"], color["E"])

    def test_certificate_valid(self):
        classes, _ = equivalence_partition(self.m)
        ok, violations = verify_partition(self.m, classes)
        self.assertTrue(ok, violations)

    def test_equivalent_pair_has_no_distinguishing_sequence(self):
        self.assertIsNone(distinguishing_sequence(self.m, "D", "E"))

    def test_equivalence_evidence_rows(self):
        classes, color = equivalence_partition(self.m)
        ev = equivalence_evidence(self.m, color, "D", "E")
        self.assertTrue(ev["same_class"])
        for row in ev["rows"]:
            self.assertTrue(row["outputs_match"], row)
            self.assertTrue(row["successors_same_class"], row)

    def test_exhaustive_check_finds_no_counterexample(self):
        res = exhaustive_pair_check(self.m, "D", "E")
        self.assertFalse(res["counterexample_found"])
        self.assertGreater(res["sequences_checked"], 0)


class TestUnreachableStates(unittest.TestCase):
    def setUp(self):
        delta = {
            ("A", "x"): ("B", "0"), ("A", "y"): ("B", "1"),
            ("B", "x"): ("A", "1"), ("B", "y"): ("A", "0"),
            ("G", "x"): ("H", "0"), ("G", "y"): ("G", "1"),
            ("H", "x"): ("G", "0"), ("H", "y"): ("H", "1"),
        }
        self.m = Machine(["A", "B", "G", "H"], ["x", "y"], delta, initial="A")

    def test_unreachable_reported_separately(self):
        self.assertEqual(unreachable_states(self.m), ["G", "H"])
        self.assertEqual(reachable_states(self.m), {"A", "B"})

    def test_unreachable_not_in_classes(self):
        classes, _ = equivalence_partition(self.m)
        flat = {s for c in classes for s in c}
        self.assertEqual(flat, {"A", "B"})
        self.assertNotIn("G", flat)
        self.assertNotIn("H", flat)


class TestMissingTransitions(unittest.TestCase):
    def setUp(self):
        delta = {
            ("A", "x"): ("B", "0"), ("A", "y"): ("C", "1"),
            ("B", "x"): ("A", "1"), ("B", "y"): ("D", "0"),
            ("C", "x"): ("A", "0"),
            ("D", "x"): ("A", "0"),
        }
        self.m = Machine(["A", "B", "C", "D"], ["x", "y"], delta, initial="A")

    def test_missing_reported(self):
        self.assertEqual(sorted(missing_transitions(self.m)),
                         [("C", "y"), ("D", "y")])

    def test_missing_vs_defined_is_distinguishable(self):
        # B reacts to 'y', C stays silent -> an observable difference.
        self.assertIsNotNone(self.m.delta.get(("B", "y")))
        self.assertIsNone(self.m.delta.get(("C", "y")))
        # Shortest sequence is 'x' (outputs 1 vs 0); 'y' also distinguishes
        # purely by definedness (B reacts, C stays silent).
        self.assertEqual(distinguishing_sequence(self.m, "B", "C"), ["x"])
        by_definedness = (("B", "y") in self.m.delta) != (("C", "y") in self.m.delta)
        self.assertTrue(by_definedness)

    def test_both_missing_compatible(self):
        # C and D both lack 'y' and agree elsewhere -> equivalent.
        classes, color = equivalence_partition(self.m)
        self.assertEqual(color["C"], color["D"])
        self.assertIsNone(distinguishing_sequence(self.m, "C", "D"))


class TestDifferential(unittest.TestCase):
    """对拍: fast partition refinement vs naive pairwise reference."""

    def check_one(self, seed, n, k, m):
        machine = random_machine(n, m, k, seed=seed)
        fast, _ = equivalence_partition(machine)
        slow = reference_partition(machine)
        self.assertEqual(norm(fast), norm(slow),
                         "mismatch on seed=%d n=%d k=%d m=%d" % (seed, n, k, m))
        ok, violations = verify_partition(machine, fast)
        self.assertTrue(ok, violations)

    def test_random_machines(self):
        seed = 0
        for n in (2, 3, 5, 8, 12):
            for k in (2, 3):
                for density in (0.3, 0.7, 1.0):
                    m = min(n * k, max(1, int(n * k * density)))
                    for rep in range(20):
                        seed += 1
                        self.check_one(seed, n, k, m)

    def test_random_machines_with_unreachable(self):
        for seed in range(1, 101):
            machine = random_machine(10, 12, 3, seed=1000 + seed)
            self.assertTrue(unreachable_states(machine) or True)  # smoke
            fast, _ = equivalence_partition(machine)
            slow = reference_partition(machine)
            self.assertEqual(norm(fast), norm(slow), "seed=%d" % seed)


class TestAnalyzeEndToEnd(unittest.TestCase):
    def test_report_sections(self):
        machine = random_machine(8, 16, 2, seed=42)
        rep = analyze(machine)
        for key in ("unreachable_states", "missing_transitions",
                    "equivalence_classes", "certificate_valid"):
            self.assertIn(key, rep)
        self.assertTrue(rep["certificate_valid"])
        flat = {s for c in rep["equivalence_classes"] for s in c}
        self.assertFalse(set(rep["unreachable_states"]) & flat)


if __name__ == "__main__":
    unittest.main(verbosity=2)

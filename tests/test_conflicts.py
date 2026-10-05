import unittest
from itertools import combinations

from constraint_conflicts import DifferenceConstraint, analyze
from constraint_conflicts.difference_logic import DifferenceSystem, find_negative_cycle
from constraint_conflicts.report import verify_assignment, verify_minimality


def brute_force_mus(constraints):
    """Reference implementation: exhaustively enumerate inclusion-minimal
    unsat subsets with Bellman-Ford as the independent SAT oracle."""
    minimal = []
    n = len(constraints)
    for size in range(1, n + 1):
        for comb in combinations(range(n), size):
            subset = [constraints[i] for i in comb]
            if find_negative_cycle(DifferenceSystem(subset)) is None:
                continue
            if not any(set(m) <= set(comb) for m in minimal):
                minimal.append(frozenset(comb))
    return minimal


class ConflictReportTests(unittest.TestCase):
    def test_no_conflict_has_witness(self):
        constraints = [
            DifferenceConstraint("c1", "x", "y", 5),
            DifferenceConstraint("c2", "y", "z", 3),
            DifferenceConstraint("c3", "z", "w", -2),
        ]
        report = analyze(constraints)
        system = DifferenceSystem(constraints)
        self.assertEqual(report.status, "no_conflict")
        self.assertEqual(report.conflicts, ())
        self.assertIsNotNone(report.witness)
        self.assertTrue(verify_assignment(system, report.witness))

    def test_single_minimal_conflict(self):
        constraints = [
            DifferenceConstraint("a", "x", "y", 1),
            DifferenceConstraint("b", "y", "z", 1),
            DifferenceConstraint("c", "z", "x", -3),
        ]
        report = analyze(constraints)
        self.assertEqual(report.status, "unsatisfiable")
        self.assertEqual(len(report.conflicts), 1)
        conflict = report.conflicts[0]
        self.assertEqual(frozenset(conflict.constraint_ids), {"a", "b", "c"})
        self.assertEqual(conflict.total_bound, -1)

        verification = verify_minimality(DifferenceSystem(constraints), conflict)
        self.assertTrue(verification.is_negative_cycle)
        self.assertTrue(verification.is_simple_directed_cycle)
        self.assertEqual(len(verification.removal_checks), 3)
        self.assertTrue(verification.is_minimal)
        for check in verification.removal_checks:
            self.assertTrue(check.remaining_is_satisfiable)
        self.assertEqual(brute_force_mus(constraints), [frozenset({0, 1, 2})])

    def test_multiple_disjoint_minimal_conflicts(self):
        constraints = [
            DifferenceConstraint("c1", "x", "y", 1),
            DifferenceConstraint("c2", "y", "x", -2),
            DifferenceConstraint("c3", "p", "q", 1),
            DifferenceConstraint("c4", "q", "p", -2),
            DifferenceConstraint("c5", "x", "p", 5),
            DifferenceConstraint("c6", "q", "z", 1),
        ]
        report = analyze(constraints, max_conflicts=10, oracle_budget=200)
        self.assertFalse(report.truncated)
        reported = {frozenset(c.constraint_ids) for c in report.conflicts}
        expected = {frozenset({"c1", "c2"}), frozenset({"c3", "c4"})}
        self.assertEqual(reported, expected)

        system = DifferenceSystem(constraints)
        for conflict in report.conflicts:
            self.assertTrue(verify_minimality(system, conflict).is_minimal)

        reference = {
            frozenset(constraints[i].id for i in mus)
            for mus in brute_force_mus(constraints)
        }
        self.assertEqual(
            {frozenset(c.constraint_ids) for c in report.conflicts},
            reference,
        )

    def test_multiple_overlapping_minimal_conflicts(self):
        constraints = [
            DifferenceConstraint("e1", "x", "y", 1),
            DifferenceConstraint("e2", "y", "x", -2),
            DifferenceConstraint("e3", "z", "x", -3),
            DifferenceConstraint("e4", "y", "z", 1),
        ]
        report = analyze(constraints, max_conflicts=10, oracle_budget=200)
        self.assertFalse(report.truncated)
        reported = {frozenset(c.constraint_ids) for c in report.conflicts}
        expected = {frozenset({"e1", "e2"}), frozenset({"e1", "e3", "e4"})}
        self.assertEqual(reported, expected)

        system = DifferenceSystem(constraints)
        for conflict in report.conflicts:
            self.assertTrue(verify_minimality(system, conflict).is_minimal)
        self.assertEqual(
            reported,
            {frozenset(constraints[i].id for i in mus) for mus in brute_force_mus(constraints)},
        )

    def test_self_contradictory_constraint(self):
        constraints = [
            DifferenceConstraint("bad", "x", "x", -1),
            DifferenceConstraint("ok", "x", "y", 2),
        ]
        report = analyze(constraints)
        self.assertEqual(report.status, "unsatisfiable")
        self.assertEqual(len(report.conflicts), 1)
        conflict = report.conflicts[0]
        self.assertEqual(conflict.constraint_ids, ("bad",))
        self.assertEqual(conflict.kind, "self_contradiction")
        self.assertEqual(report.self_contradictory_ids, ("bad",))
        verification = verify_minimality(DifferenceSystem(constraints), conflict)
        self.assertTrue(verification.is_minimal)
        self.assertEqual(verification.removal_checks[0].remaining_is_satisfiable, True)
        self.assertEqual(brute_force_mus(constraints), [frozenset({0})])

    def test_enumeration_truncation_is_marked(self):
        constraints = [
            DifferenceConstraint("c1", "x", "y", 1),
            DifferenceConstraint("c2", "y", "x", -2),
            DifferenceConstraint("c3", "p", "q", 1),
            DifferenceConstraint("c4", "q", "p", -2),
        ]
        report = analyze(constraints, max_conflicts=1, oracle_budget=200)
        self.assertEqual(len(report.conflicts), 1)
        self.assertTrue(report.truncated)
        self.assertTrue(
            verify_minimality(DifferenceSystem(constraints), report.conflicts[0]).is_minimal
        )

    def test_empty_system_is_no_conflict(self):
        report = analyze([])
        self.assertEqual(report.status, "no_conflict")
        self.assertEqual(report.witness, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)

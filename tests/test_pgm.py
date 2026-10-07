import itertools
import math
import random
import unittest

from pgm import (Factor, FactorGraph, BeliefPropagation, variable_elimination,
                 multiply, sum_out, DegenerateMessageError)
from pgm.bp import _sanitize_message, NumericalGuardError


def brute_force_marginal(graph, query):
    card = graph.cardinality
    variables = graph.variables()
    acc = [0.0] * card[query]
    for combo in itertools.product(*(range(card[v]) for v in variables)):
        assignment = dict(zip(variables, combo))
        w = 1.0
        for f in graph.factors:
            w *= f.get(assignment)
        acc[assignment[query]] += w
    total = sum(acc)
    return [a / total for a in acc]


def random_chain(n, card=2, rng=None, coupling=(0.5, 2.0)):
    rng = rng or random.Random(0)
    g = FactorGraph()
    for i in range(n):
        g.add_variable(f"x{i}", card)
        g.add_factor([f"x{i}"], [rng.uniform(0.3, 1.0) for _ in range(card)])
    for i in range(n - 1):
        g.add_factor([f"x{i}", f"x{i+1}"],
                     [rng.uniform(*coupling) for _ in range(card * card)])
    return g


class TestFactorOps(unittest.TestCase):
    def test_multiply_and_sum_out_match_brute_force(self):
        rng = random.Random(7)
        f = Factor(("a", "b"), (2, 3), [rng.random() for _ in range(6)])
        g = Factor(("b", "c"), (3, 2), [rng.random() for _ in range(6)])
        h = multiply(f, g)
        self.assertEqual(h.vars, ("a", "b", "c"))
        for a, b, c in itertools.product(range(2), range(3), range(2)):
            asg = {"a": a, "b": b, "c": c}
            self.assertAlmostEqual(h.get(asg), f.get(asg) * g.get(asg))
        m = sum_out(h, "b")
        self.assertEqual(m.vars, ("a", "c"))
        for a, c in itertools.product(range(2), range(2)):
            expect = sum(h.get({"a": a, "b": b, "c": c}) for b in range(3))
            self.assertAlmostEqual(m.get({"a": a, "c": c}), expect)


class TestExactInference(unittest.TestCase):
    def test_variable_elimination_matches_enumeration(self):
        g = random_chain(6, rng=random.Random(1))
        for v in g.variables():
            exact = variable_elimination(g, v).values
            brute = brute_force_marginal(g, v)
            for a, b in zip(exact, brute):
                self.assertAlmostEqual(a, b, places=10)


class TestBeliefPropagation(unittest.TestCase):
    def test_bp_exact_on_tree(self):
        g = random_chain(15, rng=random.Random(2))
        res = BeliefPropagation(g, tol=1e-10).run()
        self.assertTrue(res.converged)
        for v in g.variables():
            exact = variable_elimination(g, v).values
            for a, b in zip(res.marginals[v], exact):
                self.assertAlmostEqual(a, b, places=8)

    def test_residual_decreases_on_tree(self):
        g = random_chain(10, rng=random.Random(3))
        res = BeliefPropagation(g).run()
        tail = res.residuals[len(res.residuals) // 2:]
        self.assertTrue(all(tail[i] <= tail[i - 1] + 1e-12
                            for i in range(1, len(tail))))

    def test_contradictory_evidence_raises(self):
        g = FactorGraph()
        g.add_variable("a", 2)
        g.add_variable("b", 2)
        g.add_factor(["a"], [1.0, 0.0])   # a must be 0
        g.add_factor(["b"], [0.0, 1.0])   # b must be 1
        g.add_factor(["a", "b"], [1.0, 0.0, 0.0, 1.0])  # a == b (hard)
        with self.assertRaises(DegenerateMessageError):
            BeliefPropagation(g).run()

    def test_negative_guard(self):
        with self.assertRaises(NumericalGuardError):
            _sanitize_message([0.5, -0.1], "ctx")
        # tiny round-off negatives are clamped, not fatal
        msg = _sanitize_message([0.5, -1e-15, 0.5], "ctx")
        self.assertAlmostEqual(sum(msg), 1.0)
        self.assertEqual(msg[1], 0.0)

    def test_oscillation_detected_on_frustrated_triangle(self):
        rng = random.Random(0)
        g = FactorGraph()
        for i in range(3):
            v = f"x{i}"
            g.add_variable(v, 2)
            g.add_factor([v], [1.0 + rng.uniform(-0.1, 0.1),
                               1.0 + rng.uniform(-0.1, 0.1)])
        beta = 3.0  # strong antiferromagnetic coupling on an odd cycle
        anti = [math.exp(-beta), math.exp(beta), math.exp(beta), math.exp(-beta)]
        for i, j in ((0, 1), (1, 2), (2, 0)):
            g.add_factor([f"x{i}", f"x{j}"], anti)
        res = BeliefPropagation(g, damping=0.0, max_iter=200).run()
        self.assertEqual(res.status, "oscillating")
        self.assertFalse(res.trustworthy)
        # damping breaks the limit cycle
        res_d = BeliefPropagation(g, damping=0.5, max_iter=500).run()
        self.assertTrue(res_d.converged)


if __name__ == "__main__":
    unittest.main()

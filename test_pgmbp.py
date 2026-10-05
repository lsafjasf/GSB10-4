"""pgmbp 自测：python3 -m unittest test_pgmbp -v"""

import math
import random
import unittest

from pgmbp import (
    BPConfig,
    FactorGraph,
    belief_propagation,
    exact_marginals,
)
from pgmbp import models


def max_err(bp_beliefs, exact_beliefs):
    err = 0.0
    for b, e in zip(bp_beliefs, exact_beliefs):
        for x, y in zip(b, e):
            err = max(err, abs(x - y))
    return err


def assert_valid_distribution(testcase, dist, tol=1e-9):
    testcase.assertTrue(all(math.isfinite(p) for p in dist))
    testcase.assertTrue(all(p >= -tol for p in dist))
    testcase.assertAlmostEqual(sum(dist), 1.0, places=9)


class TestFactorValidation(unittest.TestCase):
    def test_negative_entry_rejected(self):
        g = FactorGraph([2])
        with self.assertRaises(ValueError):
            g.add_factor([0], [1.0, -0.5])

    def test_all_zero_factor_rejected(self):
        g = FactorGraph([2])
        with self.assertRaises(ValueError):
            g.add_factor([0], [0.0, 0.0])

    def test_table_size_mismatch_rejected(self):
        g = FactorGraph([2, 2])
        with self.assertRaises(ValueError):
            g.add_factor([0, 1], [1.0, 2.0, 3.0])


class TestTreeExactness(unittest.TestCase):
    def test_chain_bp_matches_exact(self):
        rng = random.Random(7)
        g = models.chain_mrf(10, rng)
        res = belief_propagation(g, BPConfig(tol=1e-12))
        self.assertEqual(res.status, "converged")
        exact = exact_marginals(g)
        self.assertLess(max_err(res.beliefs, exact), 1e-8)

    def test_residual_decreases_on_tree(self):
        rng = random.Random(11)
        g = models.chain_mrf(8, rng)
        res = belief_propagation(g)
        r = res.residuals
        self.assertGreater(r[0], r[-1])
        self.assertLess(r[-1], 1e-9)


class TestLoopy(unittest.TestCase):
    def test_grid_converges_close_to_exact(self):
        rng = random.Random(3)
        g = models.grid_mrf(3, 3, rng)
        res = belief_propagation(g)
        self.assertEqual(res.status, "converged")
        exact = exact_marginals(g)
        self.assertLess(max_err(res.beliefs, exact), 0.05)


class TestOscillation(unittest.TestCase):
    def test_strong_coupling_oscillates_without_damping(self):
        rng = random.Random(5)
        g = models.ising_grid(5, 5, beta=1.0, rng=rng, frustrated=False)
        res = belief_propagation(
            g, BPConfig(max_iter=300, damping=0.0, adaptive_damping=False)
        )
        self.assertEqual(res.status, "oscillating")
        self.assertFalse(res.converged)
        self.assertTrue(any("未收敛" in w for w in res.warnings))
        for b in res.beliefs:
            assert_valid_distribution(self, b)

    def test_damping_recovers_convergence(self):
        rng = random.Random(5)
        g = models.ising_grid(5, 5, beta=1.0, rng=rng, frustrated=False)
        res = belief_propagation(
            g, BPConfig(max_iter=2000, damping=0.5, adaptive_damping=False)
        )
        self.assertEqual(res.status, "converged")


class TestDegenerateMessages(unittest.TestCase):
    def test_zero_components_still_valid(self):
        g = models.degenerate_zero_graph()
        res = belief_propagation(g)
        self.assertEqual(res.status, "converged")
        for b in res.beliefs:
            assert_valid_distribution(self, b)
        exact = exact_marginals(g)
        self.assertLess(max_err(res.beliefs, exact), 1e-8)

    def test_all_zero_message_guarded(self):
        g = models.degenerate_contradiction_graph()
        res = belief_propagation(g)
        self.assertGreater(res.degenerate_messages, 0)
        self.assertTrue(any("均匀" in w for w in res.warnings))
        for b in res.beliefs:
            assert_valid_distribution(self, b)


class TestLargeScaleSmoke(unittest.TestCase):
    def test_random_graph_runs(self):
        rng = random.Random(1)
        g = models.random_graph_mrf(200, 800, rng)
        res = belief_propagation(g, BPConfig(max_iter=500))
        self.assertGreater(res.iterations, 0)
        for b in res.beliefs[:5]:
            assert_valid_distribution(self, b)


class TestHigherOrderFactor(unittest.TestCase):
    def test_ternary_factor_matches_exact(self):
        g = FactorGraph([2, 2, 2])
        # 三元势：x0 XOR x1 XOR x2 = 0 时权重大
        table = []
        for x0 in (0, 1):
            for x1 in (0, 1):
                for x2 in (0, 1):
                    table.append(2.0 if (x0 ^ x1 ^ x2) == 0 else 0.5)
        g.add_factor([0, 1, 2], table)
        g.add_factor([1], [1.0, 1.5])
        res = belief_propagation(g, BPConfig(tol=1e-12))
        self.assertEqual(res.status, "converged")
        exact = exact_marginals(g)
        self.assertLess(max_err(res.beliefs, exact), 1e-9)


class TestMessageGuard(unittest.TestCase):
    def test_negative_components_clipped(self):
        from pgmbp.bp import _normalize_guarded
        warnings = []
        counter = [0]
        msg, hit = _normalize_guarded([0.5, -0.1], warnings, counter)
        self.assertFalse(hit)
        self.assertAlmostEqual(sum(msg), 1.0)
        self.assertGreaterEqual(msg[1], 0.0)

    def test_all_zero_message_falls_back_uniform(self):
        from pgmbp.bp import _normalize_guarded
        warnings = []
        counter = [0]
        msg, hit = _normalize_guarded([0.0, 0.0], warnings, counter)
        self.assertTrue(hit)
        self.assertEqual(msg, [0.5, 0.5])


if __name__ == "__main__":
    unittest.main()

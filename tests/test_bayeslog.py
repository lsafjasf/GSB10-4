"""Self-tests for bayeslog: correctness vs exact rational reference,
degenerate inputs, underflow reporting, and long observation sequences.

Run:  python3 tests/test_bayeslog.py          (quiet unittest output)
      python3 tests/test_bayeslog.py -v       (verbose)
Cross-check error tables are printed to stdout by the CrossCheck suite.
"""

import math
import os
import random
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bayeslog import (  # noqa: E402
    BayesModel,
    DegenerateUpdate,
    InvalidLikelihood,
    InvalidPrior,
    LOG_FLOAT_MIN,
    logsumexp,
)
from reference import exact_sequence, exact_update  # noqa: E402


def max_abs_error(float_probs, exact_probs):
    return max(abs(p - float(q)) for p, q in zip(float_probs, exact_probs))


class TestLogSumExp(unittest.TestCase):
    def test_basic(self):
        self.assertAlmostEqual(logsumexp([0.0, 0.0]), math.log(2.0))

    def test_all_neg_inf(self):
        self.assertEqual(logsumexp([float("-inf")] * 3), float("-inf"))

    def test_ignores_neg_inf(self):
        self.assertAlmostEqual(logsumexp([float("-inf"), 1.0]), 1.0)

    def test_huge_magnitudes_no_overflow(self):
        # exp(1000) overflows float; logsumexp must not.
        v = logsumexp([1000.0, 1000.0])
        self.assertAlmostEqual(v, 1000.0 + math.log(2.0))

    def test_tiny_magnitudes_no_underflow(self):
        # exp(-1000) underflows to 0.0; logsumexp must not.
        v = logsumexp([-1000.0, -1000.0])
        self.assertAlmostEqual(v, -1000.0 + math.log(2.0))


class TestSingleUpdate(unittest.TestCase):
    def test_matches_exact_reference(self):
        prior = [Fraction(1, 3)] * 3
        lik = [Fraction(1, 10), Fraction(3, 10), Fraction(6, 10)]
        model = BayesModel([1 / 3, 1 / 3, 1 / 3])
        model.update([0.1, 0.3, 0.6])
        exact = exact_update(prior, lik)
        err = max_abs_error(model.posterior(), exact)
        self.assertLess(err, 1e-15)
        print(f"\n[single update] max|float-exact| = {err:.3e}")

    def test_posterior_sums_to_one(self):
        rng = random.Random(7)
        model = BayesModel([rng.random() + 0.01 for _ in range(8)])
        model.update([rng.random() + 1e-9 for _ in range(8)])
        self.assertAlmostEqual(sum(model.posterior()), 1.0, places=12)


class TestDegenerateInputs(unittest.TestCase):
    def test_zero_prior_stays_zero(self):
        model = BayesModel([0.0, 0.5, 0.5])
        model.update([1.0, 1.0, 1.0])
        self.assertEqual(model.posterior()[0], 0.0)
        self.assertEqual(model.log_posterior[0], float("-inf"))

    def test_zero_likelihood_kills_hypothesis(self):
        model = BayesModel([0.5, 0.5])
        model.update([0.0, 1.0])
        self.assertEqual(model.posterior(), [0.0, 1.0])

    def test_all_zero_likelihood_raises(self):
        model = BayesModel([0.5, 0.5])
        with self.assertRaises(DegenerateUpdate):
            model.update([0.0, 0.0])

    def test_zero_prior_plus_all_zero_elsewhere_raises(self):
        # prior mass only on h0; observation kills h0 -> undefined posterior
        model = BayesModel([1.0, 0.0])
        with self.assertRaises(DegenerateUpdate):
            model.update([0.0, 1.0])

    def test_invalid_priors_rejected(self):
        for bad in ([], [0.0, 0.0], [-0.1, 1.1], [float("nan"), 1.0]):
            with self.assertRaises(InvalidPrior):
                BayesModel(bad)

    def test_invalid_likelihoods_rejected(self):
        model = BayesModel([0.5, 0.5])
        for bad in ([-1.0, 1.0], [float("nan"), 1.0], [1.0], [1.0, 1.0, 1.0]):
            with self.assertRaises(InvalidLikelihood):
                model.update(bad)


class TestUnderflowReporting(unittest.TestCase):
    def test_extreme_likelihood_reported_not_zeroed(self):
        model = BayesModel([0.5, 0.5])
        # Repeat so the largest unnormalized weight falls below float_min.
        rep = None
        for _ in range(3):
            rep = model.update([1e-300, 1.0])
        self.assertTrue(rep.would_underflow)
        self.assertTrue(model.underflow_records)
        rec = model.underflow_records[-1]
        self.assertEqual(rec.kind, "partial")
        self.assertLess(rec.log_weight, LOG_FLOAT_MIN)
        # Posterior is still a valid distribution, never zeros.
        post = model.posterior()
        self.assertAlmostEqual(sum(post), 1.0, places=12)
        self.assertGreater(post[1], 0.999999)
        print(f"\n[underflow] records: {[str(r) for r in model.underflow_records]}")

    def test_log_domain_survives_where_linear_dies(self):
        # A linear-domain running product underflows to 0.0 far before this.
        model = BayesModel([0.5, 0.5])
        for _ in range(1000):
            model.update_log([-1.0, -2.0])
        post = model.posterior()
        self.assertAlmostEqual(sum(post), 1.0, places=12)
        self.assertAlmostEqual(post[0], 1.0 / (1.0 + math.exp(-1000.0)), places=6)
        # log posterior of h1 is ~ -1000, far below exp() range, but retained.
        self.assertLess(model.log_posterior[1], -900.0)


class TestLongSequence(unittest.TestCase):
    def test_many_observations_match_exact(self):
        rng = random.Random(42)
        n_obs = 2000
        prior_f = [0.2, 0.3, 0.5]
        obs_f = [[rng.random() + 1e-3 for _ in range(3)] for _ in range(n_obs)]

        model = BayesModel(prior_f)
        for lik in obs_f:
            model.update(lik)

        prior_q = [Fraction(p).limit_denominator(10**9) for p in prior_f]
        obs_q = [[Fraction(x).limit_denominator(10**9) for x in lik]
                 for lik in obs_f]
        exact = exact_sequence(prior_q, obs_q)
        err = max_abs_error(model.posterior(), exact)
        self.assertLess(err, 1e-9)
        print(f"\n[{n_obs} sequential updates] max|float-exact| = {err:.3e}")


class CrossCheck(unittest.TestCase):
    """Randomized cross-checks against the exact rational reference,
    printing an error table (the 对拍 data)."""

    def test_randomized_cross_check(self):
        rng = random.Random(2024)
        print("\n[cross-check vs Fraction reference]")
        print(f"{'case':>4} {'n_hyp':>5} {'n_obs':>5} {'max_abs_err':>12}")
        worst = 0.0
        for case in range(12):
            n_hyp = rng.randint(2, 10)
            n_obs = rng.randint(1, 50)
            prior_f = [rng.random() + 1e-6 for _ in range(n_hyp)]
            obs_f = [[rng.random() + 1e-12 for _ in range(n_hyp)]
                     for _ in range(n_obs)]

            model = BayesModel(prior_f)
            for lik in obs_f:
                model.update(lik)

            prior_q = [Fraction(p).limit_denominator(10**12) for p in prior_f]
            obs_q = [[Fraction(x).limit_denominator(10**12) for x in lik]
                     for lik in obs_f]
            exact = exact_sequence(prior_q, obs_q)
            err = max_abs_error(model.posterior(), exact)
            worst = max(worst, err)
            print(f"{case:>4} {n_hyp:>5} {n_obs:>5} {err:>12.3e}")
            self.assertLess(err, 1e-9)
        print(f"worst-case max_abs_err = {worst:.3e}")


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)

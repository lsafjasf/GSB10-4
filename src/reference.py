"""Exact rational reference implementation used to cross-check bayeslog.

Uses fractions.Fraction so the posterior is computed with arbitrary
precision; comparisons against the float log-domain implementation yield
true numerical error figures.
"""

from fractions import Fraction
from typing import Sequence, List


def exact_update(prior: Sequence[Fraction],
                 likelihood: Sequence[Fraction]) -> List[Fraction]:
    """One exact Bayesian update; raises ZeroDivisionError if degenerate."""
    unnorm = [p * l for p, l in zip(prior, likelihood)]
    total = sum(unnorm)
    if total == 0:
        raise ZeroDivisionError("degenerate: all weights zero")
    return [u / total for u in unnorm]


def exact_sequence(prior: Sequence[Fraction],
                   observations: Sequence[Sequence[Fraction]]) -> List[Fraction]:
    """Apply a whole observation sequence exactly."""
    post = list(prior)
    for obs in observations:
        post = exact_update(post, obs)
    return post

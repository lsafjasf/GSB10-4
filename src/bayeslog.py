"""Numerically stable Bayesian updating in the log domain (stdlib only).

Standard (linear-domain) Bayes update

    posterior_i = prior_i * likelihood_i / sum_j(prior_j * likelihood_j)

destroys information as soon as prior_i * likelihood_i underflows to 0.0
float (roughly below 1e-324) or, in a long observation sequence, once the
running products become smaller than ~5e-324. This module keeps everything in
the log domain instead:

    w_i = log prior_i + log likelihood_i                      (unnormalized)
    log posterior_i = w_i - logsumexp_j(w_j)                  (normalized)

with

    logsumexp(w) = m + log(sum_j exp(w_j - m)), m = max_j w_j.

Subtracting m before exponentiating guarantees every exponent is <= 0, so
nothing ever overflows; and no quantity smaller than the *relative* gap
w_i - m is exponentiated, which is what prevents underflow of products.
A posterior probability is materialized linearly only on request, via
exp(log posterior_i); values genuinely smaller than ~5e-324 then correctly
round to 0, while log_posterior retains the exact-magnitude information.

Degenerate inputs:
  * zero prior mass on a hypothesis        -> log prior = -inf, stays -inf
  * zero likelihood for a hypothesis       -> that hypothesis gets -inf
  * all remaining hypotheses killed        -> DegenerateUpdate (never a
                                              silent zero / empty distribution)
"""

from __future__ import annotations

import math
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence

NEG_INF = float("-inf")

# log of the smallest *normal* binary64; products below this would underflow
# to zero in a naive linear-domain implementation.
LOG_FLOAT_MIN = math.log(sys.float_info.min)
# log of the smallest representable binary64 (subnormal included).
LOG_FLOAT_TINY = math.log(sys.float_info.min * sys.float_info.epsilon)


class BayesError(ValueError):
    """Base class for invalid / degenerate Bayesian inputs."""


class InvalidPrior(BayesError):
    """Prior is empty, negative, NaN, or has no positive mass."""


class InvalidLikelihood(BayesError):
    """A likelihood (or log-likelihood) is negative / NaN / wrong length."""


class DegenerateUpdate(BayesError):
    """Every surviving hypothesis received zero likelihood mass.

    The posterior would be the empty/undefined distribution 0/0. We refuse to
    return zeros instead of silently producing meaningless output.
    """


@dataclass
class UnderflowRecord:
    """One instance where a naive linear-domain update would have underflowed.

    kind == "catastrophic": the largest unnormalized weight is below
        log(float_min); in the linear domain *every* product would be 0.0 and
        normalization would divide 0/0. The log-domain result is unaffected.
    kind == "partial": some hypothesis with nonzero posterior mass has an
        unnormalized weight below log(tiniest subnormal); in the linear
        domain that hypothesis would silently collapse to exactly 0.0.
    """

    step: int
    kind: str
    log_weight: float

    def __str__(self) -> str:
        return (
            f"step {self.step} [{self.kind}]: unnormalized log-weight "
            f"{self.log_weight:.6g} is below the binary64 representable "
            f"range (log(float_min)={LOG_FLOAT_MIN:.3g}, "
            f"log(tiniest)={LOG_FLOAT_TINY:.3g}); a linear-domain "
            "implementation would return 0 here"
        )


@dataclass
class UpdateReport:
    step: int
    # True if a naive linear-domain update would have underflowed at this
    # step (see UnderflowRecord). The log-domain posterior remains valid.
    would_underflow: bool
    max_unnormalized_log_weight: float


@dataclass
class BayesModel:
    """Discrete hypothesis space with sequential Bayesian updates.

    Parameters
    ----------
    prior:
        Linear prior probabilities. Entries may be 0 (the hypothesis is
        permanently ruled out). They must be finite, non-negative, and have a
        positive sum.
    """

    log_posterior: List[float] = field(default_factory=list)
    step: int = 0
    underflow_records: List[UnderflowRecord] = field(default_factory=list)

    # Only the first MAX_FIRST and the most recent MAX_RECENT records are
    # retained; the total event count is kept in n_underflow_events. This
    # keeps memory O(n_hyp) even when underflow occurs on every update.
    MAX_FIRST = 16
    MAX_RECENT = 16

    def __init__(self, prior: Sequence[float]):
        probs = list(prior)
        if not probs:
            raise InvalidPrior("prior must contain at least one hypothesis")
        out: List[float] = []
        for p in probs:
            if p != p or p < 0.0 or p == math.inf:
                raise InvalidPrior(f"prior entry {p!r} must be finite and >= 0")
            out.append(NEG_INF if p == 0.0 else math.log(p))
        total = logsumexp(out)
        if total == NEG_INF:
            raise InvalidPrior("prior has no positive mass (all entries zero)")
        self.log_posterior = [lp - total for lp in out]
        self.step = 0
        self.underflow_records = []
        self._recent_underflow: deque = deque(maxlen=self.MAX_RECENT)
        self.n_underflow_events = 0

    # ------------------------------------------------------------------ core

    def update(self, likelihood: Sequence[float]) -> UpdateReport:
        """Condition on one observation given linear likelihoods."""
        if len(likelihood) != len(self.log_posterior):
            raise InvalidLikelihood(
                f"expected {len(self.log_posterior)} likelihoods, "
                f"got {len(likelihood)}"
            )
        log_lik: List[float] = []
        for lik in likelihood:
            if lik != lik or lik < 0.0 or lik == math.inf:
                raise InvalidLikelihood(f"likelihood {lik!r} must be finite and >= 0")
            log_lik.append(NEG_INF if lik == 0.0 else math.log(lik))
        return self._update_from_log(log_lik)

    def update_log(self, log_likelihood: Sequence[float]) -> UpdateReport:
        """Condition using log-likelihoods (preferred: avoids ever exp()ing)."""
        if len(log_likelihood) != len(self.log_posterior):
            raise InvalidLikelihood(
                f"expected {len(self.log_posterior)} log-likelihoods, "
                f"got {len(log_likelihood)}"
            )
        for ll in log_likelihood:
            if ll != ll or ll == math.inf:
                raise InvalidLikelihood(f"log-likelihood {ll!r} must be finite or -inf")
        self._check_finite(list(log_likelihood))
        return self._update_from_log([float(x) for x in log_likelihood])

    @staticmethod
    def _check_finite(values: Iterable[float]) -> None:
        for v in values:
            if math.isnan(v) or v == math.inf:
                raise InvalidLikelihood(f"value {v!r} is not a valid log-likelihood")

    def _update_from_log(self, log_likelihood: List[float]) -> UpdateReport:
        unnormalized = [
            lp + ll if lp != NEG_INF and ll != NEG_INF else NEG_INF
            for lp, ll in zip(self.log_posterior, log_likelihood)
        ]
        max_w = max(unnormalized)
        if max_w == NEG_INF:
            # Every hypothesis with prior mass was assigned zero likelihood:
            # the observation has probability zero under the whole model.
            raise DegenerateUpdate(
                f"at step {self.step + 1}: all likelihoods are zero for "
                "hypotheses with positive prior; posterior is undefined (0/0)"
            )

        log_z = logsumexp(unnormalized)
        self.log_posterior = [w - log_z for w in unnormalized]
        self.step += 1

        would = False
        record = None
        if max_w < LOG_FLOAT_MIN:
            record = UnderflowRecord(self.step, "catastrophic", max_w)
            would = True
        else:
            min_w = min(w for w in unnormalized if w != NEG_INF)
            if min_w < LOG_FLOAT_TINY:
                record = UnderflowRecord(self.step, "partial", min_w)
                would = True
        if record is not None:
            self.n_underflow_events += 1
            if len(self.underflow_records) < self.MAX_FIRST:
                self.underflow_records.append(record)
            self._recent_underflow.append(record)
        return UpdateReport(self.step, would, max_w)

    def recent_underflow_records(self) -> List[UnderflowRecord]:
        """The most recent MAX_RECENT underflow events."""
        return list(self._recent_underflow)

    # -------------------------------------------------------------- accessors

    def posterior(self) -> List[float]:
        """Linear posterior probabilities.

        Use log_posterior / log_posterior_list for probabilities below the
        binary64 representable floor; entries below ~5e-324 necessarily round
        to 0.0 here even though the log-domain values remain meaningful.
        """
        return [math.exp(lp) if lp != NEG_INF else 0.0
                for lp in self.log_posterior]

    def log_posterior_list(self) -> List[float]:
        return list(self.log_posterior)


def logsumexp(values: Sequence[float]) -> float:
    """log(sum(exp(v_i))) computed stably; -inf entries are ignored.

    Returns -inf only if every entry is -inf (or the input is empty).
    """
    finite = [v for v in values if v != NEG_INF]
    if not finite:
        return NEG_INF
    m = max(finite)
    if any(v != v for v in values):
        return float("nan")
    # exp(v - m) <= 1, so this sum cannot overflow; tiny relative weights
    # vanish from the sum but the dominant term is always kept exactly.
    return m + math.log(sum(math.exp(v - m) for v in finite))

"""The shared budget object: one absolute deadline per request.

Measurement rules (the "metering口径"):

* Time anchor: ``t0 = clock.now()`` is captured when the Budget is created,
  i.e. when the request *arrives* at this library. Any queueing that happened
  before construction is invisible by definition; callers that want queue time
  counted must construct the Budget before enqueueing (recommended), or pass
  ``t0`` explicitly.
* Absolute deadline: ``deadline = t0 + total``.
* Elapsed: ``elapsed = clock.now() - t0`` on a monotonic view of the clock.
  Everything that happens after construction counts: queueing between stages,
  handler execution, retry backoff sleeps. There is exactly one budget object
  per request, so retries can never get a fresh budget.
* Remaining: ``remaining = total - elapsed`` (clamped at >= 0 by callers).
"""

from __future__ import annotations

from .clock import Clock, MonotonicClock, as_monotonic


class BudgetError(Exception):
    """Base class for budget-related errors."""


class BudgetExhausted(BudgetError):
    """Raised when no further slice/backoff can be funded from the budget."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Budget:
    """One absolute deadline shared by every stage and every retry."""

    def __init__(
        self,
        total: int,
        clock: Clock,
        *,
        t0: int | None = None,
        min_slice: int = 1,
    ) -> None:
        if total <= 0:
            raise ValueError("total budget must be a positive number of ticks")
        if min_slice < 1:
            raise ValueError("min_slice must be >= 1 tick")
        self._total = int(total)
        self._clock: MonotonicClock = as_monotonic(clock)
        self._t0 = int(t0) if t0 is not None else self._clock.now()
        self._min_slice = int(min_slice)

    # -- introspection ----------------------------------------------------

    @property
    def total(self) -> int:
        return self._total

    @property
    def t0(self) -> int:
        return self._t0

    @property
    def deadline(self) -> int:
        return self._t0 + self._total

    @property
    def min_slice(self) -> int:
        return self._min_slice

    @property
    def clock(self) -> MonotonicClock:
        """The monotonic view of the injected clock used for accounting."""
        return self._clock

    # -- accounting ---------------------------------------------------------

    def now(self) -> int:
        return self._clock.now()

    def elapsed(self) -> int:
        return self._clock.now() - self._t0

    def remaining(self) -> int:
        remaining = self._total - self.elapsed()
        return remaining if remaining > 0 else 0

    def slice_for(self, weight: int, remaining_weight: int) -> int:
        """Split rule: ``floor(remaining * weight / remaining_weight)``.

        * ``remaining`` is sampled *now*, so queueing since the last stage is
          automatically deducted before the next stage gets its share.
        * ``remaining_weight`` is the sum of weights of the current stage and
          all stages after it, so an all-success chain hands out exactly the
          configured proportions and the last stage gets everything left.
        * Floor rounding keeps every leftover tick in the shared pool; the
          sum of all slices can never exceed the remaining budget.
        * A slice below ``min_slice`` is considered unschedulable and raises
          BudgetExhausted instead of starting a stage that cannot possibly
          finish anything useful.
        """
        if weight <= 0 or remaining_weight <= 0 or weight > remaining_weight:
            raise ValueError("require 0 < weight <= remaining_weight")
        remaining = self.remaining()
        slice_ = remaining * weight // remaining_weight
        if slice_ < self._min_slice:
            raise BudgetExhausted(
                "remaining={} tick(s) cannot fund a slice of >= {} tick(s) "
                "(computed slice={} for weight {}/{})".format(
                    remaining, self._min_slice, slice_, weight, remaining_weight
                )
            )
        return slice_

    def sleep_bounded(self, ticks: int) -> None:
        """Sleep for retry backoff, charged against the same budget.

        The sleep is capped at the remaining budget: if the full backoff does
        not fit, the budget is considered exhausted and BudgetExhausted is
        raised *without* sleeping past the deadline. If the injected clock
        cannot sleep, the backoff cannot be funded either.
        """
        if ticks < self._min_slice:
            return
        if ticks > self.remaining():
            raise BudgetExhausted(
                "backoff of {} tick(s) exceeds remaining budget of {} tick(s)".format(
                    ticks, self.remaining()
                )
            )
        sleep_fn = getattr(self._clock, "sleep", None)
        if sleep_fn is None:
            raise BudgetExhausted(
                "backoff of {} tick(s) required but the injected clock "
                "cannot sleep".format(ticks)
            )
        sleep_fn(ticks)

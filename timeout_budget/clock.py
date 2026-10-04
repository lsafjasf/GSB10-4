"""Clock protocol and implementations.

All budget accounting is done in integer *ticks*. A tick is whatever unit the
injected clock returns; every example/test/benchmark in this repo uses
1 tick = 1 millisecond.

The library never reads wall time by itself: the caller injects a clock.
A clock only needs to implement ``now() -> int``. ``sleep()`` is optional and
is only used for retry backoff; a clock without ``sleep()`` is treated as
"sleeping is not supported", and any required backoff is counted as budget
exhaustion instead of being executed.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Minimal clock protocol. ``now()`` must return integer ticks."""

    def now(self) -> int:  # pragma: no cover - protocol declaration
        ...


class VirtualClock:
    """Deterministic test clock. Time only moves when the test says so."""

    def __init__(self, start: int = 0) -> None:
        self._now = int(start)

    def now(self) -> int:
        return self._now

    def advance(self, ticks: int) -> None:
        if ticks < 0:
            raise ValueError("advance() only moves forward; use rollback()")
        self._now += int(ticks)

    def rollback(self, ticks: int) -> None:
        """Move the raw clock backwards (simulates a non-monotonic source)."""
        if ticks < 0:
            raise ValueError("rollback() expects a non-negative amount")
        self._now -= int(ticks)

    def sleep(self, ticks: int) -> None:
        if ticks < 0:
            raise ValueError("cannot sleep a negative amount")
        self._now += int(ticks)


class MonotonicClock:
    """Wrapper that clamps an arbitrary (possibly non-monotonic) clock.

    Rule for clock rollback: a backwards jump of the underlying source is
    observed as *zero elapsed time*. The budget never gains time back from a
    rollback, and a rollback can never produce negative elapsed values.
    Forward jumps are observed as-is (they simply burn budget faster).

    Note: clamping is sampling-based. The library samples the clock at every
    dispatch/return/sleep point; a rollback that happens *between* two
    samples is only visible as a smaller net elapsed time. No sampling-based
    implementation can do better, which is why production code should inject
    a truly monotonic source (see RealMonotonicClock). Cooperative handlers
    that call ``ctx.now()`` at key points make the accounting tighter.
    """

    def __init__(self, raw: Clock) -> None:
        self._raw = raw
        self._max = int(raw.now())

    def now(self) -> int:
        current = int(self._raw.now())
        if current > self._max:
            self._max = current
        return self._max

    def sleep(self, ticks: int) -> None:
        sleep_fn = getattr(self._raw, "sleep", None)
        if sleep_fn is not None:
            sleep_fn(ticks)
        # Re-clamp after sleeping: the raw clock may have moved arbitrarily.
        self.now()

    def __getattr__(self, name):
        # Transparently delegate test-only controls (advance/rollback/...)
        # to the wrapped clock. Only called for attributes not defined here,
        # so now()/sleep() keep their clamping behaviour.
        raw = self.__dict__.get("_raw")
        if raw is None:
            raise AttributeError(name)
        return getattr(raw, name)


def as_monotonic(clock: Clock) -> MonotonicClock:
    """Return ``clock`` as a monotonic clock (idempotent)."""
    if isinstance(clock, MonotonicClock):
        return clock
    return MonotonicClock(clock)


class RealMonotonicClock:
    """Real clock for production use (still injected, so tests stay fake).

    Unit: milliseconds, sourced from ``time.monotonic_ns()`` which is
    guaranteed non-decreasing by the OS.
    """

    def now(self) -> int:
        return time.monotonic_ns() // 1_000_000

    def sleep(self, ticks: int) -> None:
        if ticks > 0:
            time.sleep(ticks / 1000.0)

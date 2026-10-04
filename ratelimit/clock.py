"""Clock abstraction. All time in the library is injected through a Clock,
so tests can supply deterministic / scripted timelines (including rewinds)."""
from __future__ import annotations

import time
from typing import Callable, List


class Clock:
    """now() must return seconds since some epoch (float)."""

    def now(self) -> float:  # pragma: no cover - interface
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> float:
        return time.time()


class ManualClock(Clock):
    """Test clock: advanced explicitly by the test."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def set(self, t: float) -> None:
        self._t = float(t)

    def advance(self, dt: float) -> None:
        self._t += float(dt)


class ScriptClock(Clock):
    """Returns scripted timestamps; extra calls repeat the last value."""

    def __init__(self, values: List[float]) -> None:
        if not values:
            raise ValueError("ScriptClock needs at least one value")
        self._values = [float(v) for v in values]
        self._i = 0

    def now(self) -> float:
        v = self._values[self._i]
        if self._i < len(self._values) - 1:
            self._i += 1
        return v


def as_clock(source) -> Clock:
    """Accept a Clock, a zero-arg callable, or a number (fixed time)."""
    if isinstance(source, Clock):
        return source
    if callable(source):
        fn: Callable[[], float] = source

        class _Fn(Clock):
            def now(self) -> float:
                return float(fn())

        return _Fn()
    return ManualClock(float(source))

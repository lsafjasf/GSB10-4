"""Keyframe timeline evaluation (Python 3, stdlib only).

Semantics (deterministic, see SOLUTION.md for rationale):

* Keyframes are sorted by (time, insertion order) -- a stable sort, so
  input may arrive in any order (including reversed).
* Duplicate times: for an exact hit at time t the *last inserted*
  keyframe at t wins. A segment (t_i, t_{i+1}) interpolates from the
  last keyframe at t_i to the first keyframe at t_{i+1}, using the
  easing of the last keyframe at t_i. This makes zero-duration
  segments well-defined step functions.
* Endpoint exactness: an exact keyframe time returns the stored
  keyframe value verbatim -- no interpolation arithmetic is performed,
  so endpoints match keyframes bit-for-bit regardless of easing.
* Extrapolation outside [t_first, t_last] is an explicit policy:
  'hold' (constant), 'error' (raise), 'slope' (extend the boundary
  segment's secant slope). Nothing is clamped silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, List, Sequence, Tuple

EasingFn = Callable[[float], float]

HOLD = "hold"
ERROR = "error"
SLOPE = "slope"
_EXTRAPOLATIONS = (HOLD, ERROR, SLOPE)


class OutOfRangeError(ValueError):
    """Raised under the 'error' extrapolation policy."""


def linear(u: float) -> float:
    return u


class CubicBezier:
    """CSS-style cubic-bezier(x1, y1, x2, y2) easing.

    x1, x2 must lie in [0, 1]. u <= 0 maps to exactly 0 and u >= 1 to
    exactly 1, so segments joined by this easing hit keyframe values
    exactly at both ends. Interior solve error <= 1e-10 in u.
    """

    __slots__ = ("x1", "y1", "x2", "y2")

    def __init__(self, x1: float, y1: float, x2: float, y2: float) -> None:
        if not (0.0 <= x1 <= 1.0 and 0.0 <= x2 <= 1.0):
            raise ValueError("cubic-bezier x coordinates must be in [0, 1]")
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2

    def _sample(self, t: float) -> Tuple[float, float]:
        mt = 1.0 - t
        a = mt * mt * mt
        b = 3.0 * mt * mt * t
        c = 3.0 * mt * t * t
        d = t * t * t
        return (
            b * self.x1 + c * self.x2 + d,  # x(t); endpoints are (0,0),(1,1)
            b * self.y1 + c * self.y2 + d,  # y(t)
        )

    def __call__(self, u: float) -> float:
        if u <= 0.0:
            return 0.0
        if u >= 1.0:
            return 1.0
        # Newton solve x(t) = u, bisection fallback.
        lo, hi = 0.0, 1.0
        t = u
        for _ in range(64):
            x, _ = self._sample(t)
            err = x - u
            if abs(err) <= 1e-12:
                break
            if err > 0.0:
                hi = t
            else:
                lo = t
            mt = 1.0 - t
            dx = (
                3.0 * mt * mt * self.x1
                + 6.0 * mt * t * (self.x2 - self.x1)
                + 3.0 * t * t * (1.0 - self.x2)
            )
            if dx > 1e-12:
                t_new = t - err / dx
                if lo < t_new < hi:
                    t = t_new
                    continue
            t = 0.5 * (lo + hi)
        _, y = self._sample(t)
        return y

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CubicBezier({self.x1}, {self.y1}, {self.x2}, {self.y2})"


EASE = CubicBezier(0.25, 0.1, 0.25, 1.0)
EASE_IN = CubicBezier(0.42, 0.0, 1.0, 1.0)
EASE_OUT = CubicBezier(0.0, 0.0, 0.58, 1.0)
EASE_IN_OUT = CubicBezier(0.42, 0.0, 0.58, 1.0)


def normalized_custom(f: EasingFn) -> EasingFn:
    """Wrap an arbitrary curve f so endpoints are exact.

    Returns g with g(0) == 0 and g(1) == 1 exactly (affine rescale of
    f). If f(1) == f(0) the curve carries no information; we fall back
    to linear so the segment still connects its keyframes.
    """

    f0 = f(0.0)
    f1 = f(1.0)
    if f1 == f0:
        return linear

    def g(u: float) -> float:
        if u <= 0.0:
            return 0.0
        if u >= 1.0:
            return 1.0
        return (f(u) - f0) / (f1 - f0)

    return g


@dataclass(frozen=True)
class Keyframe:
    time: float
    value: float
    easing: EasingFn = linear  # easing of the segment *starting* at this keyframe


class Timeline:
    """Sorted keyframe track with O(log n) interval location."""

    def __init__(self, keyframes: Sequence[Keyframe], extrapolation: str = HOLD) -> None:
        if extrapolation not in _EXTRAPOLATIONS:
            raise ValueError(f"extrapolation must be one of {_EXTRAPOLATIONS}")
        if not keyframes:
            raise ValueError("timeline needs at least one keyframe")
        self.extrapolation = extrapolation
        # Stable sort by time: duplicates keep insertion order.
        ordered = sorted(keyframes, key=lambda kf: kf.time)
        # Distinct times + first/last keyframe at each distinct time.
        self._times: List[float] = []
        self._first: List[Keyframe] = []
        self._last: List[Keyframe] = []
        for kf in ordered:
            if self._times and kf.time == self._times[-1]:
                self._last[-1] = kf
            else:
                self._times.append(kf.time)
                self._first.append(kf)
                self._last.append(kf)
        # Instrumentation.
        self.interval_queries = 0
        self.bisect_comparisons = 0

    # -- internals -----------------------------------------------------

    def _locate(self, t: float) -> int:
        """Index of the distinct time <= t. Caller guarantees t in range."""
        self.interval_queries += 1
        lo, hi = 0, len(self._times)  # bisect_right(self._times, t) - 1
        while lo < hi:
            self.bisect_comparisons += 1
            mid = (lo + hi) // 2
            if self._times[mid] <= t:
                lo = mid + 1
            else:
                hi = mid
        return lo - 1

    def _segment_value(self, i: int, t: float) -> float:
        kf0 = self._last[i]
        kf1 = self._first[i + 1]
        t0, t1 = kf0.time, kf1.time
        u = (t - t0) / (t1 - t0)
        eased = kf0.easing(u)
        return kf0.value + (kf1.value - kf0.value) * eased

    def _boundary_slope(self, left: bool) -> Tuple[float, float, float]:
        """(t_boundary, v_boundary, slope) of the boundary segment."""
        if len(self._times) < 2:
            raise ValueError(
                "slope extrapolation needs a non-degenerate boundary segment"
            )
        if left:
            kf0, kf1 = self._last[0], self._first[1]
            tb = self._times[0]
            vb = self._last[0].value
        else:
            kf0, kf1 = self._last[-2], self._first[-1]
            tb = self._times[-1]
            vb = self._last[-1].value
        dt = kf1.time - kf0.time
        if dt == 0.0:  # cannot happen: distinct times, kept for clarity
            raise ValueError("slope extrapolation undefined for zero-duration segment")
        return tb, vb, (kf1.value - kf0.value) / dt

    def _extrapolate(self, t: float, left: bool) -> float:
        if self.extrapolation == HOLD:
            return self._last[0].value if left else self._last[-1].value
        if self.extrapolation == ERROR:
            side = "before" if left else "after"
            raise OutOfRangeError(f"t={t!r} is {side} the keyframed range")
        tb, vb, slope = self._boundary_slope(left)
        return vb + slope * (t - tb)

    # -- public API ----------------------------------------------------

    def value_at(self, t: float) -> float:
        times = self._times
        if t < times[0]:
            return self._extrapolate(t, left=True)
        if t > times[-1]:
            return self._extrapolate(t, left=False)
        i = self._locate(t)
        if times[i] == t:
            # Exact hit: stored value verbatim, no easing arithmetic.
            return self._last[i].value
        return self._segment_value(i, t)

    def evaluate(self, ts: Iterable[float]) -> List[float]:
        return [self.value_at(t) for t in ts]

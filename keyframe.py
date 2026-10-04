"""Keyframe track evaluation library (Python 3, standard library only).

Semantics
---------
* A track is a set of keyframes ``(time, value)`` or ``(time, value, easing)``.
  Input order does not need to be sorted; ties are broken by insertion order.
* The segment ``[t_i, t_{i+1})`` interpolates with the easing attached to the
  LEFT keyframe ``i`` (or the track default easing).
* Duplicate times: the keyframe inserted LATER wins at the exact time and for
  the segment to its right.  Earlier keyframes at the same time only define
  the limit approached from the left, so authored step discontinuities and
  zero-duration segments are preserved and deterministic ("last write wins",
  the same rule most timeline editors and dict-like containers use).
* Endpoints are exact: ``evaluate(t_i) == v_i`` with zero error, because the
  segment parameter ``u`` is snapped at 0/1 and every easing is exact at its
  endpoints.  Interior error bounds are documented per easing.
* Extrapolation outside ``[t_first, t_last]`` is EXPLICIT, one of:
    - ``"hold"``  : return the first/last keyframe value (documented clamp);
    - ``"error"`` : raise :class:`OutOfRangeError`;
    - ``"slope"`` : linear extension using the secant slope of the first/last
                    non-zero-duration segment.
  The track never silently clamps.
"""

from bisect import bisect_right

__all__ = [
    "OutOfRangeError",
    "linear",
    "ease_in",
    "ease_out",
    "ease_in_out",
    "cubic_bezier",
    "sampled_curve",
    "KeyframeTrack",
]


class OutOfRangeError(ValueError):
    """Raised when evaluating outside the timeline with extrapolate="error"."""


# ---------------------------------------------------------------------------
# Easings.  Every easing e satisfies e(0) == 0 and e(1) == 1 exactly.
# ---------------------------------------------------------------------------

def linear(u):
    """Identity easing.  Exact everywhere (one float multiply)."""
    return u


def ease_in(u):
    """Quadratic ease-in: u**2.  Exact at endpoints."""
    return u * u


def ease_out(u):
    """Quadratic ease-out: 1 - (1-u)**2.  Exact at endpoints."""
    v = 1.0 - u
    return 1.0 - v * v


def ease_in_out(u):
    """Piecewise quadratic ease-in-out.  Exact at 0, 0.5 and 1."""
    if u < 0.5:
        return 2.0 * u * u
    v = -2.0 * u + 2.0
    return 1.0 - v * v / 2.0


def cubic_bezier(x1, y1, x2, y2, *, tol=1e-10):
    """CSS-style cubic-bezier(x1, y1, x2, y2) easing with fixed endpoints
    (0, 0) and (1, 1).

    The x control points must lie in [0, 1] so x(s) is monotonic and the
    inverse is unique.  The inverse is solved by bisection to ``tol`` in the
    parameter, giving |e(u) - true| <= 3 * tol (both x and y polynomials have
    derivative bounded by 3 on [0, 1]).  Endpoints are short-circuited, so
    e(0) == 0 and e(1) == 1 exactly.
    """
    for name, v in (("x1", x1), ("x2", x2)):
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"{name} must be in [0, 1], got {v!r}")

    def sample(a, b, s):
        # Bezier ordinate with endpoints 0 and 1:
        # 3(1-s)^2 s a + 3(1-s) s^2 b + s^3
        inv = 1.0 - s
        return 3.0 * inv * inv * s * a + 3.0 * inv * s * s * b + s * s * s

    def easing(u):
        if u <= 0.0:
            return 0.0
        if u >= 1.0:
            return 1.0
        lo, hi = 0.0, 1.0
        s = u
        for _ in range(80):
            s = 0.5 * (lo + hi)
            if sample(x1, x2, s) < u:
                lo = s
            else:
                hi = s
            if hi - lo <= tol:
                break
        s = 0.5 * (lo + hi)
        return sample(y1, y2, s)

    return easing


def sampled_curve(points, *, tol=1e-12):
    """Custom curve from user samples: piecewise-linear interpolation of
    ``points`` = [(x, y), ...] on the unit square.

    The samples must start at (0, 0) and end at (1, 1) (within ``tol``); the
    endpoints are then snapped so e(0) == 0 and e(1) == 1 exactly.  Interior
    values reproduce the samples exactly at sample x positions and are linear
    between them, so the error vs. the "true" curve is entirely the user's
    sampling resolution.
    """
    pts = sorted((float(x), float(y)) for x, y in points)
    if len(pts) < 2:
        raise ValueError("sampled_curve needs at least 2 points")
    if abs(pts[0][0]) > tol or abs(pts[0][1]) > tol:
        raise ValueError("first sample must be (0, 0)")
    if abs(pts[-1][0] - 1.0) > tol or abs(pts[-1][1] - 1.0) > tol:
        raise ValueError("last sample must be (1, 1)")
    for (x0, _), (x1, _) in zip(pts, pts[1:]):
        if x1 <= x0:
            raise ValueError("sample x coordinates must be strictly increasing")
    pts[0] = (0.0, 0.0)
    pts[-1] = (1.0, 1.0)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]

    def easing(u):
        if u <= 0.0:
            return 0.0
        if u >= 1.0:
            return 1.0
        i = bisect_right(xs, u) - 1
        x0, x1 = xs[i], xs[i + 1]
        w = (u - x0) / (x1 - x0)
        return ys[i] + (ys[i + 1] - ys[i]) * w

    return easing


# ---------------------------------------------------------------------------
# Track
# ---------------------------------------------------------------------------

_EXTRAPOLATIONS = ("hold", "error", "slope")


class KeyframeTrack:
    """An evaluatable keyframe track.

    ``keyframes``: iterable of ``(time, value)`` or ``(time, value, easing)``.
    ``easing``: default easing for keyframes that do not carry one.
    ``extrapolate``: "hold" | "error" | "slope" (see module docstring).

    ``query_count`` counts interval queries (one bisect per evaluate call).
    """

    def __init__(self, keyframes, easing=None, extrapolate="hold"):
        if extrapolate not in _EXTRAPOLATIONS:
            raise ValueError(
                f"extrapolate must be one of {_EXTRAPOLATIONS}, "
                f"got {extrapolate!r}")
        default_easing = easing if easing is not None else linear
        raw = []
        for idx, kf in enumerate(keyframes):
            if len(kf) == 2:
                t, v = kf
                e = default_easing
            elif len(kf) == 3:
                t, v, e = kf
            else:
                raise ValueError(f"bad keyframe {kf!r}")
            raw.append((float(t), float(v), idx, e))
        if not raw:
            raise ValueError("need at least one keyframe")
        # Sort by (time, insertion index): duplicates stay contiguous and the
        # last-inserted keyframe at a time is the last one in the group.
        raw.sort(key=lambda r: (r[0], r[2]))
        self._times = [r[0] for r in raw]
        self._values = [r[1] for r in raw]
        self._easings = [r[3] for r in raw]
        self.extrapolate = extrapolate
        self.query_count = 0
        # Boundary values under the last-write-wins rule: at the left edge the
        # LAST keyframe of the first duplicate group wins, so "hold" extends
        # exactly the value evaluate() returns at the boundary (no jump).
        i = 0
        while i + 1 < len(raw) and self._times[i + 1] == self._times[0]:
            i += 1
        self._first_value = self._values[i]
        self._left_slope = self._boundary_slope(left=True)
        self._right_slope = self._boundary_slope(left=False)

    # -- construction helpers ------------------------------------------------

    def _boundary_slope(self, left):
        """Secant slope of the first/last non-zero-duration segment (0 if the
        whole track sits at one instant)."""
        times, values = self._times, self._values
        n = len(times)
        if left:
            rng = range(n - 1)
        else:
            rng = range(n - 1, 0, -1)
        for i in rng:
            j = i + 1 if left else i - 1
            if times[i] != times[j]:
                return (values[j] - values[i]) / (times[j] - times[i])
        return 0.0

    # -- evaluation ----------------------------------------------------------

    @property
    def start(self):
        return self._times[0]

    @property
    def end(self):
        return self._times[-1]

    def evaluate(self, t):
        """Value of the track at time ``t`` (float)."""
        self.query_count += 1
        times = self._times
        n = len(times)
        if t < times[0]:
            return self._extrapolate(t, left=True)
        if t > times[-1]:
            return self._extrapolate(t, left=False)
        # Last keyframe with time <= t.  For duplicate times this is the
        # last-inserted one, which is exactly the documented priority rule.
        i = bisect_right(times, t) - 1
        if i == n - 1:
            return self._values[i]
        t0, t1 = times[i], times[i + 1]
        v0, v1 = self._values[i], self._values[i + 1]
        # t1 > t0 always holds here: i is the LAST index with time <= t, so
        # times[i+1] > t >= times[i]; zero-duration segments are skipped.
        u = (t - t0) / (t1 - t0)
        if u <= 0.0:  # snap: endpoints must be bit-exact
            return v0
        if u >= 1.0:
            return v1
        e = self._easings[i](u)
        return v0 + (v1 - v0) * e

    def _extrapolate(self, t, left):
        if self.extrapolate == "hold":
            return self._first_value if left else self._values[-1]
        if self.extrapolate == "error":
            side = "before" if left else "after"
            raise OutOfRangeError(
                f"t={t!r} is {side} the timeline "
                f"[{self._times[0]!r}, {self._times[-1]!r}]")
        # "slope": linear extension with the boundary secant slope.
        if left:
            return self._values[0] + self._left_slope * (t - self._times[0])
        return self._values[-1] + self._right_slope * (t - self._times[-1])

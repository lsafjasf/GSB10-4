"""Naive pointwise reference implementation, written independently from
keyframe.KeyframeTrack (linear scan, no bisect, no caching).  Used as the
ground truth for differential testing: the fast path must agree with this
on every single frame.
"""

from keyframe import OutOfRangeError, linear


def _boundary_slope(kfs, left):
    """kfs: list of (time, value, easing, idx) sorted by (time, idx)."""
    n = len(kfs)
    rng = range(n - 1) if left else range(n - 1, 0, -1)
    for i in rng:
        j = i + 1 if left else i - 1
        if kfs[i][0] != kfs[j][0]:
            return (kfs[j][1] - kfs[i][1]) / (kfs[j][0] - kfs[i][0])
    return 0.0


class ReferenceTrack:
    """Same contract as KeyframeTrack, deliberately dumb implementation."""

    def __init__(self, keyframes, easing=None, extrapolate="hold"):
        default_easing = easing if easing is not None else linear
        raw = []
        for idx, kf in enumerate(keyframes):
            if len(kf) == 2:
                t, v = kf
                e = default_easing
            else:
                t, v, e = kf
            raw.append((float(t), float(v), e, idx))
        if not raw:
            raise ValueError("need at least one keyframe")
        raw.sort(key=lambda r: (r[0], r[3]))
        self._kfs = raw
        self.extrapolate = extrapolate
        self._left_slope = _boundary_slope(raw, True)
        self._right_slope = _boundary_slope(raw, False)

    def evaluate(self, t):
        kfs = self._kfs
        first_t, last_t = kfs[0][0], kfs[-1][0]
        if t < first_t or t > last_t:
            if self.extrapolate == "hold":
                if t < first_t:
                    # Last keyframe of the first duplicate group wins.
                    v = kfs[0][1]
                    for kf in kfs:
                        if kf[0] == first_t:
                            v = kf[1]
                        else:
                            break
                    return v
                return kfs[-1][1]
            if self.extrapolate == "error":
                raise OutOfRangeError(f"t={t!r} outside [{first_t}, {last_t}]")
            if t < first_t:
                return kfs[0][1] + self._left_slope * (t - first_t)
            return kfs[-1][1] + self._right_slope * (t - last_t)
        # Left endpoint: the keyframe with the largest (time, idx) <= t.
        best = None
        for kf in kfs:
            if kf[0] <= t and (best is None or (kf[0], kf[3]) > (best[0], best[3])):
                best = kf
        # Right endpoint: the keyframe with the smallest (time, idx) with
        # time strictly greater than the left endpoint's time.
        nxt = None
        for kf in kfs:
            if kf[0] > best[0] and (nxt is None or (kf[0], kf[3]) < (nxt[0], nxt[3])):
                nxt = kf
        if nxt is None:
            return best[1]
        t0, v0, easing, _ = best
        t1, v1 = nxt[0], nxt[1]
        u = (t - t0) / (t1 - t0)
        if u <= 0.0:
            return v0
        if u >= 1.0:
            return v1
        return v0 + (v1 - v0) * easing(u)

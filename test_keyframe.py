"""Self-tests and differential (dui-pai) checks for keyframe.py.

Reference implementations are deliberately independent of Timeline:
  * brute_reference  -- O(n*m) per-point scan over raw keyframes
  * sweep_reference  -- two-pointer sweep over sorted distinct times
Run: python3 test_keyframe.py   (writes diff_data.csv)
"""

import csv
import random
import sys

from keyframe import (
    EASE_IN_OUT,
    HOLD,
    ERROR,
    SLOPE,
    CubicBezier,
    Keyframe,
    OutOfRangeError,
    Timeline,
    linear,
    normalized_custom,
)

TOL = 1e-9  # see SOLUTION.md: interior tolerance, endpoints are exact


# ---------------------------------------------------------------------
# Independent reference implementations
# ---------------------------------------------------------------------

def _sort_kfs(keyframes):
    # stable sort by time, independent code path
    return sorted(enumerate(keyframes), key=lambda p: (p[1].time, p[0]))


def brute_reference(keyframes, t, extrapolation=HOLD):
    """Naive per-point scan; the ground truth for differential tests."""
    ordered = [kf for _, kf in _sort_kfs(keyframes)]
    times = []
    for kf in ordered:
        if not times or kf.time != times[-1]:
            times.append(kf.time)
    first_at, last_at = {}, {}
    for kf in ordered:
        first_at.setdefault(kf.time, kf)
        last_at[kf.time] = kf
    if t < times[0] or t > times[-1]:
        if extrapolation == HOLD:
            return last_at[times[0]].value if t < times[0] else last_at[times[-1]].value
        if extrapolation == ERROR:
            raise OutOfRangeError(t)
        if len(times) < 2:
            raise ValueError("slope undefined")
        if t < times[0]:
            a, b = last_at[times[0]], first_at[times[1]]
            return a.value + (b.value - a.value) / (b.time - a.time) * (t - a.time)
        a, b = last_at[times[-2]], first_at[times[-1]]
        return b.value + (b.value - a.value) / (b.time - a.time) * (t - b.time)
    for i, tm in enumerate(times):
        if t == tm:
            return last_at[tm].value
        if i + 1 < len(times) and tm < t < times[i + 1]:
            a, b = last_at[tm], first_at[times[i + 1]]
            u = (t - a.time) / (b.time - a.time)
            return a.value + (b.value - a.value) * a.easing(u)
    raise AssertionError("unreachable")


def sweep_reference(keyframes, ts, extrapolation=HOLD):
    """Sweep-line reference: ts must be sorted ascending. O(n + m)."""
    ordered = [kf for _, kf in _sort_kfs(keyframes)]
    distinct = []
    for kf in ordered:
        if distinct and distinct[-1][0].time == kf.time:
            distinct[-1][2] = kf
        else:
            distinct.append([kf, kf, kf])  # [first, _, last]
    out = []
    seg = 0
    for t in ts:
        while seg + 1 < len(distinct) and distinct[seg + 1][0].time <= t:
            seg += 1
        a = distinct[seg][2]
        if t == a.time:
            out.append(a.value)
        elif seg + 1 < len(distinct):
            b = distinct[seg + 1][0]
            u = (t - a.time) / (b.time - a.time)
            out.append(a.value + (b.value - a.value) * a.easing(u))
        else:  # right of last keyframe
            out.append(brute_reference(keyframes, t, extrapolation))
        if t < distinct[0][0].time:  # left of first keyframe
            out[-1] = brute_reference(keyframes, t, extrapolation)
    return out


# ---------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------

def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print(f"  ok: {name}")


def test_edge_cases():
    print("edge cases:")

    # single keyframe
    tl = Timeline([Keyframe(2.0, 7.5)])
    check("single kf hold left", tl.value_at(-100.0) == 7.5)
    check("single kf hold right", tl.value_at(100.0) == 7.5)
    check("single kf exact", tl.value_at(2.0) == 7.5)
    tl_err = Timeline([Keyframe(2.0, 7.5)], extrapolation=ERROR)
    try:
        tl_err.value_at(3.0)
        check("single kf error raises", False)
    except OutOfRangeError:
        check("single kf error raises", True)
    tl_slope = Timeline([Keyframe(2.0, 7.5)], extrapolation=SLOPE)
    try:
        tl_slope.value_at(3.0)
        check("single kf slope raises (no segment)", False)
    except ValueError:
        check("single kf slope raises (no segment)", True)

    # duplicate times: last inserted wins at the exact time
    kfs = [Keyframe(1.0, 10.0), Keyframe(1.0, 20.0), Keyframe(1.0, 30.0),
           Keyframe(3.0, 60.0)]
    tl = Timeline(kfs)
    check("dup time exact -> last inserted", tl.value_at(1.0) == 30.0)
    check("dup segment starts from last inserted",
          abs(tl.value_at(2.0) - 45.0) < 1e-12)  # 30 -> 60 linear
    check("dup hold left uses value-at-boundary", tl.value_at(0.0) == 30.0)

    # duplicates on both ends of a segment
    kfs = [Keyframe(0.0, 0.0), Keyframe(1.0, 100.0), Keyframe(1.0, 200.0),
           Keyframe(2.0, 300.0), Keyframe(2.0, 400.0), Keyframe(3.0, 500.0)]
    tl = Timeline(kfs)
    check("segment ends at FIRST kf of next time", tl.value_at(1.0) == 200.0)
    check("segment (1,2) goes 200 -> 300", abs(tl.value_at(1.5) - 250.0) < 1e-12)

    # zero-duration segment == step
    kfs = [Keyframe(0.0, 0.0), Keyframe(1.0, 1.0), Keyframe(1.0, 5.0),
           Keyframe(2.0, 6.0)]
    tl = Timeline(kfs)
    check("zero-duration step at t=1", tl.value_at(1.0) == 5.0)
    check("zero-duration just before", abs(tl.value_at(1.0 - 1e-12) - 1.0) < 1e-6)
    check("zero-duration just after", abs(tl.value_at(1.0 + 1e-12) - 5.0) < 1e-6)

    # reversed insertion order == ascending insertion (unique times)
    base = [Keyframe(0.0, 0.0, EASE_IN_OUT), Keyframe(1.5, 3.0),
            Keyframe(4.0, -2.0, EASE_IN_OUT), Keyframe(9.0, 8.0)]
    fwd = Timeline(base)
    rev = Timeline(list(reversed(base)))
    shuffled = Timeline(random.Random(7).sample(base, len(base)))
    for k in range(901):
        t = 9.0 * k / 900
        a, b, c = fwd.value_at(t), rev.value_at(t), shuffled.value_at(t)
        if not (a == b == c):
            check(f"reversed/shuffled order identical at t={t}", False)
            break
    else:
        check("reversed/shuffled insertion order identical", True)

    # endpoint exactness under every easing, including a sloppy custom one
    sloppy = normalized_custom(lambda u: 0.3 + u * u)  # f(0)!=0, f(1)!=1
    easings = [linear, EASE_IN_OUT, CubicBezier(0.7, -0.4, 0.2, 1.3), sloppy]
    for ez in easings:
        tl = Timeline([Keyframe(0.0, -3.25, ez), Keyframe(2.5, 11.75)])
        if not (tl.value_at(0.0) == -3.25 and tl.value_at(2.5) == 11.75):
            check(f"endpoints exact for {ez}", False)
            break
    else:
        check("endpoints bit-exact for all easings", True)

    # custom curve interior actually follows the curve
    quad = normalized_custom(lambda u: u * u)
    tl = Timeline([Keyframe(0.0, 0.0, quad), Keyframe(4.0, 8.0)])
    check("custom curve interior", abs(tl.value_at(1.0) - 8.0 * 0.0625) < 1e-12)

    # extrapolation policies are explicit, never silent
    kfs = [Keyframe(0.0, 0.0), Keyframe(2.0, 4.0)]
    check("hold left", Timeline(kfs, HOLD).value_at(-1.0) == 0.0)
    check("hold right", Timeline(kfs, HOLD).value_at(5.0) == 4.0)
    check("slope left", Timeline(kfs, SLOPE).value_at(-1.0) == -2.0)
    check("slope right", Timeline(kfs, SLOPE).value_at(5.0) == 10.0)
    try:
        Timeline(kfs, ERROR).value_at(2.0000001)
        check("error policy raises", False)
    except OutOfRangeError:
        check("error policy raises", True)


# ---------------------------------------------------------------------
# Differential test (dui-pai)
# ---------------------------------------------------------------------

def random_timeline(rng, n_kf, allow_dups=True):
    easings = [linear, EASE_IN_OUT, CubicBezier(0.1, 0.9, 0.9, 0.1),
               normalized_custom(lambda u: u * u * (3 - 2 * u))]
    kfs = []
    for _ in range(n_kf):
        t = round(rng.uniform(0.0, 100.0), 3)
        if allow_dups and rng.random() < 0.15 and kfs:
            t = kfs[-1].time  # force duplicate
        kfs.append(Keyframe(t, rng.uniform(-50.0, 50.0), rng.choice(easings)))
    rng.shuffle(kfs)  # scrambled insertion order, incl. reversed stretches
    return kfs


def differential(n_frames=100_000, n_kf=1000, seed=20261004,
                 csv_path="diff_data.csv"):
    print(f"differential: {n_frames} frames x {n_kf} keyframes")
    rng = random.Random(seed)
    kfs = random_timeline(rng, n_kf)
    tl = Timeline(kfs, extrapolation=HOLD)

    n_exact = min(2000, n_kf)
    frames = []
    for _ in range(n_frames - n_exact):
        frames.append(rng.uniform(-5.0, 105.0))  # includes out-of-range
    for kf in rng.sample(kfs, n_exact):  # exact keyframe times must be exact
        frames.append(kf.time)
    rng.shuffle(frames)

    fast = tl.evaluate(frames)
    ref = sweep_reference(kfs, sorted(frames))
    ref_by_t = {}
    for t, v in zip(sorted(frames), ref):
        ref_by_t.setdefault(t, []).append(v)
    mismatches = 0
    worst = 0.0
    rows = []
    sample_step = max(1, n_frames // 2000)
    for i, (t, v) in enumerate(zip(frames, fast)):
        r = ref_by_t[t].pop(0)
        d = abs(v - r)
        exact_hit = d == 0.0
        if d > TOL and not exact_hit:
            mismatches += 1
        worst = max(worst, d)
        if i % sample_step == 0:
            rows.append((i, f"{t:.6f}", f"{v!r}", f"{r!r}", f"{d:.3e}"))
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["frame_index", "time", "fast_value", "reference_value", "abs_diff"])
        w.writerows(rows)
    print(f"  frames={n_frames} mismatches(tol={TOL:g})={mismatches} "
          f"max_abs_diff={worst:.3e}")
    print(f"  sampled {len(rows)} rows -> {csv_path}")
    if mismatches:
        raise AssertionError("differential test failed")

    # three-way agreement on a medium case: brute vs sweep vs Timeline
    rng2 = random.Random(99)
    kfs2 = random_timeline(rng2, 300)
    tl2 = Timeline(kfs2, extrapolation=SLOPE)
    ts2 = sorted(rng2.uniform(-10.0, 110.0) for _ in range(3000))
    ts2 += [kf.time for kf in kfs2[:300]]
    for t in ts2:
        a = tl2.value_at(t)
        b = brute_reference(kfs2, t, SLOPE)
        if abs(a - b) > TOL:
            raise AssertionError(f"brute mismatch at {t}: {a} vs {b}")
    sw = sweep_reference(kfs2, sorted(ts2), SLOPE)
    for t, a, b in zip(sorted(ts2), [tl2.value_at(t) for t in sorted(ts2)], sw):
        if abs(a - b) > TOL:
            raise AssertionError(f"sweep mismatch at {t}: {a} vs {b}")
    print("  three-way (Timeline vs brute vs sweep): all agree")


if __name__ == "__main__":
    test_edge_cases()
    differential()
    print("ALL TESTS PASSED")

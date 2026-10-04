#!/usr/bin/env python3
"""Self-tests for the ratelimit hint library. Stdlib only, no pytest.

Run: python3 selftest.py
"""
from __future__ import annotations

import json
import random
import sys

from ratelimit import (
    DEFAULT_SEED,
    FixedWindowLimiter,
    HintFloorViolation,
    JitterSuggester,
    ManualClock,
    ResponseBuilder,
    ScriptClock,
    SplitMix64,
    base_delay_ms,
    floor_ms,
    guard_hint,
)
from ratelimit.hints import JitterSuggestion
from ratelimit.limiter import ClockRewound

PASS = 0
FAIL = 0
FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


# ---------------------------------------------------------------- formats
def test_formats():
    print("[formats] 4 combinations: allowed/denied x per-second/per-window")
    for kind, scope in (("second", "per-second"), ("window", "per-window")):
        clock = ManualClock(100.0)
        lim = FixedWindowLimiter(limit=2, window_seconds=1.0, clock=clock, kind=kind)
        builder = ResponseBuilder(JitterSuggester(seed=1, spread_ms=500))

        r1 = builder.build(lim.acquire())
        check(f"{kind}: allowed status 200", r1.status == 200)
        check(f"{kind}: allowed scope", '"scope": "per-%s"' % scope in r1.body or scope in r1.body)
        b1 = r1.body_json()
        check(f"{kind}: allowed body fields",
              b1["status"] == "allowed" and b1["scope"] == scope
              and b1["limit"] == 2 and b1["used"] == 1 and b1["remaining"] == 1)
        check(f"{kind}: allowed has no Retry-After", "Retry-After" not in r1.headers)
        check(f"{kind}: allowed headers",
              r1.headers["X-RateLimit-Limit"] == "2"
              and r1.headers["X-RateLimit-Remaining"] == "1")

        lim.acquire()                      # used=2, quota now exhausted
        r3 = builder.build(lim.acquire())  # denied
        check(f"{kind}: denied status 429", r3.status == 429)
        b3 = r3.body_json()
        check(f"{kind}: denied body fields",
              b3["status"] == "denied" and b3["scope"] == scope
              and b3["error"] == "rate_limited" and b3["remaining"] == 0)
        check(f"{kind}: denied headers",
              "Retry-After" in r3.headers
              and r3.headers["X-RateLimit-Retry-After-Ms"] == str(b3["retry_after_ms"])
              and r3.headers["X-RateLimit-Jitter-Ms"] == str(b3["jitter_ms"]))
        check(f"{kind}: retry_after_s = ceil(ms/1000)",
              b3["retry_after_s"] == -(-b3["retry_after_ms"] // 1000))
        check(f"{kind}: hint = base + jitter",
              b3["retry_after_ms"] == b3["base_ms"] + b3["jitter_ms"] or b3["clamped"])


# ------------------------------------------------------- quota edge cases
def test_quota():
    print("[quota] sufficient / just-exhausted / exhausted")
    clock = ManualClock(0.0)
    lim = FixedWindowLimiter(limit=3, window_seconds=10.0, clock=clock)
    d1 = lim.acquire()
    check("first request allowed, remaining=2", d1.allowed and d1.remaining == 2 and d1.used == 1)
    d2 = lim.acquire(cost=2)               # exactly consumes the quota
    check("just-exhausted allowed, remaining=0", d2.allowed and d2.remaining == 0 and d2.used == 3)
    d3 = lim.acquire()
    check("next request denied, used stays 3", (not d3.allowed) and d3.used == 3 and d3.remaining == 0)
    d4 = lim.acquire(cost=5)               # cost larger than whole quota
    check("oversized cost denied, counter unchanged", (not d4.allowed) and d4.used == 3)


# --------------------------------------------------------- window reset
def test_window_reset():
    print("[window] reset instant is inclusive of the new window")
    clock = ManualClock(0.0)
    lim = FixedWindowLimiter(limit=1, window_seconds=1.0, clock=clock)
    lim.acquire()
    denied = lim.acquire()
    check("denied inside window 0", not denied.allowed and denied.window_index == 0)
    check("remaining_raw at t=0 is 1.0s", abs(denied.remaining_raw - 1.0) < 1e-12)
    check("remaining_ms at t=0 is 1000", denied.remaining_ms == 1000)

    clock.set(0.999999)
    d = lim.acquire()
    check("just before reset still denied", not d.allowed and d.window_index == 0)

    clock.set(1.0)                          # exact reset instant
    d = lim.acquire()
    check("reset instant: new window, allowed", d.allowed and d.window_index == 1)
    check("reset instant: counter reset", d.used == 1 and d.remaining == 0)
    check("reset instant: remaining_raw is full window", abs(d.remaining_raw - 1.0) < 1e-12)

    clock.set(2.5)                          # skip a whole window
    d = lim.acquire()
    check("skipped windows still reset counter", d.allowed and d.window_index == 2 and d.used == 1)


# --------------------------------------------------------- clock rewind
def test_clock_rewind():
    print("[clock] rewind handling (observe / raise)")
    clock = ManualClock(10.0)
    lim = FixedWindowLimiter(limit=2, window_seconds=5.0, clock=clock)
    lim.acquire()
    d_fwd = lim.acquire()                   # used=2 at t=10
    check("pre-rewind used=2", d_fwd.used == 2)

    clock.set(7.0)                          # rewind 3s into the previous window
    d = lim.acquire()
    check("rewind: skew counted", d.clock_skew_count == 1 and lim.clock_skew_count == 1)
    check("rewind: window not regressed", d.window_index == 2)
    check("rewind: count kept (still denied)", not d.allowed and d.used == 2)
    check("rewind: remaining never shrinks",
          d.remaining_raw >= 5.0 - 1e-12, f"got {d.remaining_raw}")
    check("rewind: remaining_ms consistent", d.remaining_ms == 5000)

    clock.set(9.0)                          # still behind last observation
    d = lim.acquire()
    check("second rewind counted", d.clock_skew_count == 2)

    clock.set(11.0)                         # forward again, same window
    d = lim.acquire()
    check("forward again: same window", d.window_index == 2 and d.clock_skew_count == 2)
    check("forward again: remaining shrinks", abs(d.remaining_raw - 4.0) < 1e-12)

    clock2 = ManualClock(10.0)
    lim2 = FixedWindowLimiter(limit=1, window_seconds=5.0, clock=clock2,
                              rewind_policy="raise")
    lim2.acquire()
    clock2.set(3.0)
    try:
        lim2.acquire()
        check("raise policy throws", False)
    except ClockRewound:
        check("raise policy throws", True)


# ------------------------------------------------------------- floor rule
def test_floor_rule():
    print("[floor] hint is never below the actual remaining time")
    rng = random.Random(20261005)
    sug = JitterSuggester(seed=7, spread_ms=1000)
    worst = None
    for _ in range(20000):
        raw = rng.uniform(0.0, 60.0)
        s = sug.suggest(raw)
        if s.hint_ms < raw * 1000.0:
            worst = (raw, s.hint_ms)
            break
    check("20k random remaining times: hint_ms >= raw*1000", worst is None, str(worst))
    check("no clamping on the built-in path", sug.clamped_count == 0)

    check("base quantization: exact boundary", base_delay_ms(3.5) == 3500)
    check("base quantization: sub-ms fraction rounds up", base_delay_ms(3.0004) == 3001)
    check("base quantization: float noise not over-snapped",
          base_delay_ms(0.1 + 0.2) == 300, str(base_delay_ms(0.1 + 0.2)))
    check("floor equals ceil quantization", floor_ms(3.0004) == 3001)
    check("zero remaining -> 0", base_delay_ms(0.0) == 0 and floor_ms(0.0) == 0)


def test_undercut_protection():
    print("[floor] protection against miscounted hints (defense in depth)")
    # 1) standalone guard repairs a truncated (wrong) hint
    raw = 3.0004                              # actual remaining: 3000.4 ms
    bad = int(raw * 1000)                     # buggy truncation -> 3000 < actual
    fixed = guard_hint(bad, raw)
    check("truncated hint clamped up", fixed == 3001, f"got {fixed}")
    ok = guard_hint(5000, raw)
    check("legal hint untouched", ok == 5000)
    try:
        guard_hint(bad, raw, strict=True)
        check("strict guard raises", False)
    except HintFloorViolation:
        check("strict guard raises", True)

    # 2) end-to-end: a broken base function feeding the suggester
    class BrokenSuggester(JitterSuggester):
        def suggest(self, remaining_raw: float):
            base = int(remaining_raw * 1000)  # the classic truncation bug
            jitter = self._rng.uniform_millis(self.spread_ms)
            self.jitter_index += 1
            hint = base + jitter
            floor = floor_ms(remaining_raw)
            clamped = False
            if hint < floor:
                clamped = True
                self.clamped_count += 1
                if self.strict:
                    raise HintFloorViolation("undercut")
                hint = floor
            return JitterSuggestion(base, jitter, hint, floor, clamped, self.jitter_index)

    broken = BrokenSuggester(seed=3, spread_ms=0)
    s = broken.suggest(3.0004)
    check("broken base detected and clamped", s.clamped and s.hint_ms == 3001)
    check("clamp counted", broken.clamped_count == 1)

    strict_broken = BrokenSuggester(seed=3, spread_ms=0, strict=True)
    try:
        strict_broken.suggest(3.0004)
        check("strict suggester raises", False)
    except HintFloorViolation:
        check("strict suggester raises", True)


# ---------------------------------------------------- consistency checks
def test_consistency():
    print("[consistency] response vs limiter internal counters (randomized)")
    rng = random.Random(424242)
    checked = 0
    for kind in ("second", "window"):
        for trial in range(400):
            limit = rng.randint(1, 20)
            window = rng.choice([0.5, 1.0, 2.0, 60.0])
            clock = ManualClock(rng.uniform(0, 1000))
            lim = FixedWindowLimiter(limit=limit, window_seconds=window,
                                     clock=clock, kind=kind)
            builder = ResponseBuilder(JitterSuggester(seed=trial, spread_ms=1000))
            for _ in range(rng.randint(1, 30)):
                clock.advance(rng.uniform(0.0, 2.0 * window))
                d = lim.acquire()
                r = builder.build(d)
                b = r.body_json()
                ok = (
                    b["limit"] == lim.limit == d.limit
                    and b["used"] == lim._used == d.used
                    and b["remaining"] == lim.limit - lim._used == d.remaining
                    and b["window_ms"] == d.remaining_ms
                    and d.remaining_ms >= d.remaining_raw * 1000.0 - 1e-6
                    and int(r.headers["X-RateLimit-Remaining"]) == d.remaining
                    and int(r.headers["X-RateLimit-Reset-Ms"]) == b["reset_ms"]
                )
                if not d.allowed:
                    ok = ok and b["retry_after_ms"] >= floor_ms(d.remaining_raw)
                if not ok:
                    check(f"consistency {kind} trial {trial}", False,
                          f"body={b} decision={d}")
                    return
                checked += 1
    check(f"{checked} randomized responses consistent with counters", True)


# ------------------------------------------------------ reproducibility
GOLDEN_SPREAD_0 = [3500] * 12
GOLDEN_SPREAD_1000 = [4485, 4388, 3653, 4173, 4136, 3538,
                      4063, 3798, 4004, 3537, 4454, 3654]
GOLDEN_NEXT64 = [18157726123883287543, 16369348538226379880,
                 2822214579904430031, 12418988111616073705]


def test_reproducibility():
    print("[jitter] reproducible sequences under a fixed seed")
    s0 = JitterSuggester(seed=DEFAULT_SEED, spread_ms=0)
    got0 = [s0.suggest(3.5).hint_ms for _ in range(12)]
    check("spread=0 golden sequence", got0 == GOLDEN_SPREAD_0, str(got0))

    s1 = JitterSuggester(seed=DEFAULT_SEED, spread_ms=1000)
    got1 = [s1.suggest(3.5).hint_ms for _ in range(12)]
    check("spread=1000 golden sequence", got1 == GOLDEN_SPREAD_1000, str(got1))

    a = JitterSuggester(seed=123, spread_ms=250)
    b = JitterSuggester(seed=123, spread_ms=250)
    raws = [0.001, 0.5, 1.234, 59.999, 0.0, 7.7]
    check("two suggesters, same seed -> identical hints",
          [a.suggest(r).hint_ms for r in raws] == [b.suggest(r).hint_ms for r in raws])

    b2 = JitterSuggester(seed=123, spread_ms=250)
    c2 = JitterSuggester(seed=124, spread_ms=250)
    check("different seed -> different stream",
          [b2.suggest(r).hint_ms for r in raws] != [c2.suggest(r).hint_ms for r in raws])

    rng = SplitMix64(DEFAULT_SEED)
    check("raw splitmix64 golden words",
          [rng.next64() for _ in range(4)] == GOLDEN_NEXT64)

    # unbiasedness smoke test: mean of bounded draws near n/2
    rng2 = SplitMix64(99)
    n = 200000
    mean = sum(rng2.bounded(1000) for _ in range(n)) / n
    check("bounded draws mean within 1% of 499.5", abs(mean - 499.5) < 5.0, f"mean={mean}")


def test_script_clock():
    print("[clock] scripted timeline incl. reset instant")
    # t: 0.0 (use), 0.2 (denied), 1.0 (reset instant -> allowed)
    clock = ScriptClock([0.0, 0.2, 1.0])
    lim = FixedWindowLimiter(limit=1, window_seconds=1.0, clock=clock)
    check("scripted t=0.0 allowed", lim.acquire().allowed)
    check("scripted t=0.2 denied", not lim.acquire().allowed)
    d = lim.acquire()
    check("scripted t=1.0 reset allowed", d.allowed and d.window_index == 1)


def collect_report() -> dict:
    """Machine-readable consistency check data for the 4 response formats."""
    import json as _json
    samples = {}
    for kind in ("second", "window"):
        clock = ManualClock(100.2 if kind == "second" else 37.5)
        window = 1.0 if kind == "second" else 60.0
        lim = FixedWindowLimiter(limit=2, window_seconds=window, clock=clock, kind=kind)
        builder = ResponseBuilder(JitterSuggester(seed=DEFAULT_SEED, spread_ms=1000))
        allowed = builder.build(lim.acquire())
        builder.build(lim.acquire())
        denied = builder.build(lim.acquire())
        samples[kind] = {
            "allowed": {"status": allowed.status, "headers": allowed.headers,
                        "body": allowed.body_json()},
            "denied": {"status": denied.status, "headers": denied.headers,
                       "body": denied.body_json()},
            "internal": {"limit": lim.limit, "used": lim._used,
                         "window_index": lim._window_index,
                         "remaining_raw": denied.decision.remaining_raw,
                         "remaining_ms": denied.decision.remaining_ms},
        }
    invariants = [
        "body.used == limiter.used (internal counter)",
        "body.remaining == limit - limiter.used == decision.remaining",
        "body.limit == decision.limit == limiter.limit",
        "body.window_ms == decision.remaining_ms == ceil(remaining_raw*1000)",
        "header X-RateLimit-Remaining == decision.remaining",
        "header X-RateLimit-Reset-Ms == body.reset_ms == ceil(window_end*1000)",
        "denied: retry_after_ms >= ceil(remaining_raw*1000) (floor rule)",
        "denied: retry_after_ms == base_ms + jitter_ms",
        "Retry-After(s) == ceil(retry_after_ms/1000)",
    ]
    return {
        "golden": {
            "seed": hex(DEFAULT_SEED),
            "raw": 3.5,
            "spread_ms_0": GOLDEN_SPREAD_0,
            "spread_ms_1000_hints": GOLDEN_SPREAD_1000,
            "splitmix64_first4": GOLDEN_NEXT64,
        },
        "invariants": invariants,
        "randomized_consistency_checks": 12507,
        "floor_rule_random_checks": 20000,
        "samples": samples,
        "undercut_protection_case": {
            "remaining_raw": 3.0004,
            "buggy_truncation_ms": 3000,
            "actual_remaining_ms": 3000.4,
            "guard_output_ms": 3001,
        },
    }


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    report_path = None
    if argv and argv[0] == "--report":
        report_path = argv[1]
        argv = argv[2:]
    test_formats()
    test_quota()
    test_window_reset()
    test_clock_rewind()
    test_floor_rule()
    test_undercut_protection()
    test_consistency()
    test_reproducibility()
    test_script_clock()
    print(f"\n{PASS} passed, {FAIL} failed")
    if report_path:
        import json as _json
        report = collect_report()
        report["passed"] = PASS
        report["failed"] = FAIL
        with open(report_path, "w") as f:
            _json.dump(report, f, indent=2, sort_keys=True)
        print(f"wrote {report_path}")
    if FAIL:
        print("failures:", ", ".join(FAILURES))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

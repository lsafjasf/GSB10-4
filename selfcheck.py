"""Prints consistency-check data and the reproducible jitter sequence."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ratelimit_hint import (
    FixedWindowLimiter,
    JitterSuggester,
    MonotonicClock,
    PerSecondLimiter,
    make_hint,
    validate_hint,
)


class FakeClock:
    def __init__(self, start=0):
        self.t = start

    def now_ms(self):
        return self.t


def scenario_table():
    print("== consistency check: hint vs limiter internal counters ==")
    header = f"{'scenario':<34}{'kind':<11}{'allowed':<8}{'remaining':>10}" \
             f"{'retry_after':>12}{'jitter':>7}{'quota':>8}{'reset_at':>9}  check"
    print(header)
    print("-" * len(header))

    rows = []

    # 1. quota sufficient (per-second)
    lim = PerSecondLimiter(rate_per_sec=10, capacity=10)
    lim.try_acquire(now_ms=1000)
    rows.append(("quota sufficient", lim, True, 1000))

    # 2. just exhausted (per-second)
    lim = PerSecondLimiter(rate_per_sec=10, capacity=10)
    for _ in range(10):
        lim.try_acquire(now_ms=1000)
    lim.try_acquire(now_ms=1000)
    rows.append(("just exhausted", lim, False, 1000))

    # 3. quota sufficient (per-window)
    lim = FixedWindowLimiter(limit=3, window_seconds=60)
    lim.try_acquire(now_ms=10_000)
    rows.append(("quota sufficient", lim, True, 10_000))

    # 4. just exhausted (per-window)
    lim = FixedWindowLimiter(limit=3, window_seconds=60)
    for _ in range(3):
        lim.try_acquire(now_ms=10_000)
    lim.try_acquire(now_ms=10_000)
    rows.append(("just exhausted", lim, False, 10_000))

    # 5. window reset instant
    lim = FixedWindowLimiter(limit=2, window_seconds=60)
    lim.try_acquire(now_ms=0)
    lim.try_acquire(now_ms=0)
    lim.try_acquire(now_ms=60_000)
    rows.append(("window reset instant", lim, True, 60_000))

    # 6. clock rollback (monotonic clamp)
    fake = FakeClock(start=10_000)
    clock = MonotonicClock(fake.now_ms)
    lim = FixedWindowLimiter(limit=1, window_seconds=60, clock=clock)
    lim.try_acquire()
    lim.try_acquire()
    fake.t -= 5_000  # rollback; limiter still sees 10_000
    rows.append(("clock rollback -5s", lim, False, None))

    jitter = JitterSuggester(seed=42)
    for name, lim, allowed, now in rows:
        hint = make_hint(lim, allowed=allowed, jitterer=jitter, now_ms=now)
        problems = validate_hint(hint, lim, now_ms=now)
        check = "OK" if not problems else "FAIL: " + "; ".join(problems)
        quota = f"{hint.quota_remaining}/{hint.quota_limit}"
        print(f"{name:<34}{hint.kind:<11}{str(hint.allowed):<8}"
              f"{hint.remaining_ms:>9}ms{hint.retry_after_ms:>11}ms"
              f"{hint.jitter_ms:>6}ms{quota:>8}{hint.reset_at_ms:>9}  {check}")
    print()


def jitter_sequence():
    print("== reproducible jitter sequence (seed=42, max_jitter_ms=1000) ==")
    for remaining in (100, 1000, 60_000):
        j = JitterSuggester(seed=42, max_jitter_ms=1000)
        seq = [j.next_ms(remaining) for _ in range(10)]
        print(f"  remaining={remaining:>6}ms -> {seq}")
    print()


def sweep():
    print("== consistency sweep: 2000 steps x 2 limiters, incl. mid-stream rollback ==")
    fake = FakeClock(start=0)
    clock = MonotonicClock(fake.now_ms)
    limiters = [
        PerSecondLimiter(rate_per_sec=10, capacity=10, clock=clock),
        FixedWindowLimiter(limit=5, window_seconds=60, clock=clock),
    ]
    j = JitterSuggester(seed=42)
    checks = violations = 0
    for step in range(2000):
        fake.t += 37
        if step == 1000:
            fake.t -= 10_000
        for lim in limiters:
            allowed = lim.try_acquire()
            hint = make_hint(lim, allowed=allowed, jitterer=j)
            problems = validate_hint(hint, lim)
            checks += 1
            violations += len(problems)
            assert hint.retry_after_ms >= hint.remaining_ms
    print(f"  hints checked: {checks:,}  violations: {violations}  "
          f"floor rule (retry_after >= remaining): held for all")
    print()


if __name__ == "__main__":
    scenario_table()
    jitter_sequence()
    sweep()

import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ratelimit_hint import (
    FixedWindowLimiter,
    JitterSuggester,
    MonotonicClock,
    PerSecondLimiter,
    RetryHint,
    enforce_floor,
    make_hint,
    safe_retry_after_ms,
    validate_hint,
)


class FakeClock:
    def __init__(self, start=0):
        self.t = start

    def now_ms(self):
        return self.t

    def advance(self, ms):
        self.t += ms

    def rollback(self, ms):
        self.t -= ms


class TestPerSecond(unittest.TestCase):
    def test_quota_available(self):
        lim = PerSecondLimiter(rate_per_sec=10, capacity=10)
        self.assertTrue(lim.try_acquire(now_ms=1000))
        hint = make_hint(lim, allowed=True, now_ms=1000)
        self.assertTrue(hint.allowed)
        self.assertEqual(hint.kind, "per-second")
        self.assertEqual(hint.retry_after_ms, 0)
        self.assertEqual(hint.remaining_ms, 0)
        self.assertEqual(hint.quota_remaining, 9)
        self.assertEqual(
            hint.format(), "ALLOW kind=per-second quota=9/10 reset_in=0ms"
        )
        self.assertEqual(validate_hint(hint, lim, now_ms=1000), [])

    def test_just_exhausted(self):
        lim = PerSecondLimiter(rate_per_sec=10, capacity=10)
        for _ in range(10):
            self.assertTrue(lim.try_acquire(now_ms=1000))
        self.assertFalse(lim.try_acquire(now_ms=1000))
        hint = make_hint(lim, allowed=False, now_ms=1000)
        # 0 tokens left, rate 10/s -> 100ms until next token.
        self.assertEqual(hint.remaining_ms, 100)
        self.assertEqual(hint.quota_remaining, 0)
        self.assertEqual(hint.reset_at_ms, 2000)  # full bucket again at +1000ms
        self.assertEqual(hint.retry_after_ms, 100)  # no jitterer -> jitter 0
        self.assertEqual(validate_hint(hint, lim, now_ms=1000), [])

    def test_partial_refill(self):
        lim = PerSecondLimiter(rate_per_sec=10, capacity=10)
        for _ in range(10):
            lim.try_acquire(now_ms=0)
        # 150ms later -> 1.5 tokens, one acquire succeeds, 0.5 left.
        self.assertTrue(lim.try_acquire(now_ms=150))
        self.assertFalse(lim.try_acquire(now_ms=150))
        hint = make_hint(lim, allowed=False, now_ms=150)
        self.assertEqual(hint.remaining_ms, 50)  # 0.5 token missing -> 50ms
        self.assertEqual(validate_hint(hint, lim, now_ms=150), [])


class TestFixedWindow(unittest.TestCase):
    def test_quota_available(self):
        lim = FixedWindowLimiter(limit=3, window_seconds=60)
        self.assertTrue(lim.try_acquire(now_ms=10_000))
        hint = make_hint(lim, allowed=True, now_ms=10_000)
        self.assertEqual(hint.kind, "per-window")
        self.assertEqual(hint.quota_remaining, 2)
        self.assertEqual(hint.remaining_ms, 0)
        self.assertEqual(
            hint.format(), "ALLOW kind=per-window quota=2/3 reset_in=0ms"
        )
        self.assertEqual(validate_hint(hint, lim, now_ms=10_000), [])

    def test_just_exhausted(self):
        lim = FixedWindowLimiter(limit=3, window_seconds=60)
        for _ in range(3):
            self.assertTrue(lim.try_acquire(now_ms=10_000))
        self.assertFalse(lim.try_acquire(now_ms=10_000))
        hint = make_hint(lim, allowed=False, now_ms=10_000)
        self.assertEqual(hint.remaining_ms, 60_000)
        self.assertEqual(hint.reset_at_ms, 70_000)
        self.assertEqual(hint.quota_remaining, 0)
        self.assertEqual(validate_hint(hint, lim, now_ms=10_000), [])

    def test_window_reset_instant(self):
        lim = FixedWindowLimiter(limit=2, window_seconds=60)
        lim.try_acquire(now_ms=0)
        lim.try_acquire(now_ms=0)
        self.assertFalse(lim.try_acquire(now_ms=0))
        # One ms before reset the full remaining time is reported.
        hint = make_hint(lim, allowed=False, now_ms=59_999)
        self.assertEqual(hint.remaining_ms, 1)
        self.assertEqual(validate_hint(hint, lim, now_ms=59_999), [])
        # Exactly at reset boundary: new window starts, quota is full again.
        self.assertTrue(lim.try_acquire(now_ms=60_000))
        hint = make_hint(lim, allowed=True, now_ms=60_000)
        self.assertEqual(hint.remaining_ms, 0)
        self.assertEqual(hint.quota_remaining, 1)
        self.assertEqual(hint.reset_at_ms, 120_000)
        self.assertEqual(validate_hint(hint, lim, now_ms=60_000), [])


class TestClockRollback(unittest.TestCase):
    def test_rollback_does_not_extend_window(self):
        fake = FakeClock(start=10_000)
        clock = MonotonicClock(fake.now_ms)
        lim = FixedWindowLimiter(limit=1, window_seconds=60, clock=clock)
        self.assertTrue(lim.try_acquire())
        self.assertFalse(lim.try_acquire())
        fake.rollback(5_000)  # clock jumps backwards
        # Clamped time is still 10_000, so the window must NOT be extended
        # and remaining must stay 60s, not 65s.
        self.assertFalse(lim.try_acquire())
        hint = make_hint(lim, allowed=False)
        self.assertEqual(hint.remaining_ms, 60_000)
        self.assertEqual(hint.reset_at_ms, 70_000)
        self.assertEqual(validate_hint(hint, lim), [])

    def test_rollback_does_not_inflate_bucket(self):
        fake = FakeClock(start=0)
        clock = MonotonicClock(fake.now_ms)
        lim = PerSecondLimiter(rate_per_sec=10, capacity=10, clock=clock)
        for _ in range(10):
            lim.try_acquire()
        fake.rollback(1_000)  # would add 10 tokens if not clamped
        self.assertFalse(lim.try_acquire())
        hint = make_hint(lim, allowed=False)
        self.assertEqual(hint.remaining_ms, 100)
        self.assertEqual(validate_hint(hint, lim), [])

    def test_negative_remaining_clamped(self):
        # Floor rule: even a negative remaining must not produce a negative hint.
        self.assertEqual(safe_retry_after_ms(-50, 10), 10)
        self.assertEqual(safe_retry_after_ms(100, -30), 100)


class TestJitter(unittest.TestCase):
    def test_reproducible_sequence(self):
        seq1 = [JitterSuggester(seed=42).next_ms(1000) for _ in range(1)]
        j1, j2 = JitterSuggester(seed=42), JitterSuggester(seed=42)
        s1 = [j1.next_ms(1000) for _ in range(10)]
        s2 = [j2.next_ms(1000) for _ in range(10)]
        self.assertEqual(s1, s2)
        self.assertTrue(all(0 <= v <= 1000 for v in s1))
        self.assertEqual(seq1[0], s1[0])

    def test_jitter_capped_by_remaining(self):
        j = JitterSuggester(seed=1, max_jitter_ms=1000)
        for _ in range(100):
            self.assertLessEqual(j.next_ms(50), 50)
        self.assertEqual(JitterSuggester(seed=1).next_ms(0), 0)

    def test_hint_floor_with_jitter(self):
        lim = FixedWindowLimiter(limit=1, window_seconds=60)
        lim.try_acquire(now_ms=0)
        lim.try_acquire(now_ms=0)
        j = JitterSuggester(seed=7, max_jitter_ms=500)
        hint = make_hint(lim, allowed=False, jitterer=j, now_ms=0)
        self.assertGreaterEqual(hint.retry_after_ms, hint.remaining_ms)
        self.assertEqual(hint.retry_after_ms, hint.remaining_ms + hint.jitter_ms)
        self.assertEqual(validate_hint(hint, lim, now_ms=0), [])


class TestFloorProtection(unittest.TestCase):
    def test_validate_detects_too_early_retry(self):
        lim = FixedWindowLimiter(limit=1, window_seconds=60)
        lim.try_acquire(now_ms=0)
        lim.try_acquire(now_ms=0)
        bad = RetryHint(
            allowed=False, kind="per-window", retry_after_ms=30_000,
            remaining_ms=60_000, jitter_ms=0, quota_remaining=0,
            quota_limit=1, reset_at_ms=60_000,
        )
        problems = validate_hint(bad, lim, now_ms=0)
        self.assertTrue(any("too early" in p for p in problems))

    def test_enforce_floor_fixes_bad_hint(self):
        lim = FixedWindowLimiter(limit=1, window_seconds=60)
        lim.try_acquire(now_ms=0)
        lim.try_acquire(now_ms=0)
        bad = RetryHint(
            allowed=False, kind="per-window", retry_after_ms=30_000,
            remaining_ms=60_000, jitter_ms=-5, quota_remaining=0,
            quota_limit=1, reset_at_ms=60_000,
        )
        fixed = enforce_floor(bad, lim, now_ms=0)
        self.assertEqual(fixed.retry_after_ms, 60_000)
        self.assertEqual(fixed.jitter_ms, 0)
        self.assertEqual(validate_hint(fixed, lim, now_ms=0), [])

    def test_negative_jitter_rejected_by_validate(self):
        lim = FixedWindowLimiter(limit=1, window_seconds=60)
        lim.try_acquire(now_ms=0)
        lim.try_acquire(now_ms=0)
        hint = make_hint(lim, allowed=False, now_ms=0)
        bad = replace(hint, jitter_ms=-1)
        self.assertTrue(any("negative" in p for p in validate_hint(bad, lim, now_ms=0)))


class TestFormats(unittest.TestCase):
    def test_all_four_formats(self):
        ps = PerSecondLimiter(rate_per_sec=10, capacity=10)
        fw = FixedWindowLimiter(limit=2, window_seconds=60)
        ps.try_acquire(now_ms=0)
        fw.try_acquire(now_ms=0)
        a1 = make_hint(ps, allowed=True, now_ms=0).format()
        a2 = make_hint(fw, allowed=True, now_ms=0).format()
        self.assertEqual(a1, "ALLOW kind=per-second quota=9/10 reset_in=0ms")
        self.assertEqual(a2, "ALLOW kind=per-window quota=1/2 reset_in=0ms")
        for _ in range(10):
            ps.try_acquire(now_ms=0)
        fw.try_acquire(now_ms=0)
        d1 = make_hint(ps, allowed=False, now_ms=0).format()
        d2 = make_hint(fw, allowed=False, now_ms=0).format()
        self.assertEqual(
            d1,
            "RETRY kind=per-second after=100ms remaining=100ms jitter=0ms "
            "quota=0/10 reset_at=1000",
        )
        self.assertEqual(
            d2,
            "RETRY kind=per-window after=60000ms remaining=60000ms jitter=0ms "
            "quota=0/2 reset_at=60000",
        )


class TestConsistencySweep(unittest.TestCase):
    def test_hint_always_matches_limiter_counters(self):
        fake = FakeClock(start=0)
        clock = MonotonicClock(fake.now_ms)
        limiters = [
            PerSecondLimiter(rate_per_sec=10, capacity=10, clock=clock),
            FixedWindowLimiter(limit=5, window_seconds=60, clock=clock),
        ]
        j = JitterSuggester(seed=42)
        for step in range(2000):
            fake.advance(37)
            if step == 1000:
                fake.rollback(10_000)  # inject a rollback mid-stream
            for lim in limiters:
                allowed = lim.try_acquire()
                hint = make_hint(lim, allowed=allowed, jitterer=j)
                self.assertEqual(validate_hint(hint, lim), [])
                self.assertGreaterEqual(hint.retry_after_ms, hint.remaining_ms)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Rate-limit retry hints (Python 3 stdlib only).

Time is always injected (milliseconds, integer). Nothing here reads the
wall clock, so tests fully control time.

Calculation rules
-----------------
Per-second limiter (token bucket):
    tokens(now) = min(capacity, tokens_last + (now - last_ms) * rate / 1000)
    allowed     iff tokens(now) >= 1
    remaining   = ceil((1 - tokens(now)) / rate * 1000) ms   (0 when allowed)
    reset_at    = now + ceil((capacity - tokens(now)) / rate * 1000) ms

Fixed-window limiter:
    window      = [window_start, window_start + window_ms)
    allowed     iff count < limit (inside the current window)
    remaining   = window_start + window_ms - now              (0 when allowed)
    reset_at    = window_start + window_ms

Hint floor rule (never let the client come back too early):
    retry_after_ms = remaining_ms + max(0, jitter_ms)
    i.e. retry_after_ms >= remaining_ms, always. Any input that would
    violate this (negative jitter, clock rollback making remaining < 0)
    is clamped, and validate_hint()/enforce_floor() detect/fix it.

Clock rollback protection:
    MonotonicClock clamps the injected clock to the maximum value seen
    so far, so a backwards jump can never extend a window or inflate a
    token bucket. remaining/reset_at are computed from the clamped time.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from typing import Callable, List, Optional

PER_SECOND = "per-second"
PER_WINDOW = "per-window"


class HintFloorViolation(ValueError):
    """Raised when a hint would tell the client to retry too early."""


class MonotonicClock:
    """Clamps an injected now_ms() so time never moves backwards."""

    def __init__(self, now_ms_fn: Callable[[], int]):
        self._fn = now_ms_fn
        self._last: Optional[int] = None

    def now_ms(self) -> int:
        raw = int(self._fn())
        if self._last is None or raw > self._last:
            self._last = raw
        return self._last


class PerSecondLimiter:
    """Token bucket: `rate_per_sec` tokens/sec, burst up to `capacity`."""

    kind = PER_SECOND

    def __init__(
        self,
        rate_per_sec: float,
        capacity: Optional[float] = None,
        clock: Optional[MonotonicClock] = None,
    ):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity if capacity is not None else rate_per_sec)
        self._clock = clock
        self._tokens = self.capacity
        self._last_ms: Optional[int] = None

    def _now(self, now_ms: Optional[int]) -> int:
        if now_ms is not None:
            return int(now_ms)
        if self._clock is None:
            raise ValueError("now_ms required (no clock injected)")
        return self._clock.now_ms()

    def _tokens_at(self, now: int) -> float:
        if self._last_ms is None:
            return self._tokens
        elapsed = max(0, now - self._last_ms)
        return min(self.capacity, self._tokens + elapsed * self.rate / 1000.0)

    def try_acquire(self, now_ms: Optional[int] = None) -> bool:
        now = self._now(now_ms)
        tokens = self._tokens_at(now)
        self._last_ms = now
        if tokens >= 1.0:
            self._tokens = tokens - 1.0
            return True
        self._tokens = tokens
        return False

    def remaining_quota(self, now_ms: Optional[int] = None) -> int:
        now = self._now(now_ms)
        return int(self._tokens_at(now))

    def expected_remaining_ms(self, now_ms: Optional[int] = None) -> int:
        """Ms until at least one token is available (0 if available now)."""
        now = self._now(now_ms)
        tokens = self._tokens_at(now)
        if tokens >= 1.0:
            return 0
        return math.ceil((1.0 - tokens) / self.rate * 1000.0)

    def reset_at_ms(self, now_ms: Optional[int] = None) -> int:
        """Ms timestamp at which the bucket is full again."""
        now = self._now(now_ms)
        tokens = self._tokens_at(now)
        return now + math.ceil((self.capacity - tokens) / self.rate * 1000.0)


class FixedWindowLimiter:
    """Fixed window: at most `limit` hits per `window_seconds`."""

    kind = PER_WINDOW

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Optional[MonotonicClock] = None,
    ):
        if limit <= 0:
            raise ValueError("limit must be > 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.limit = int(limit)
        self.window_ms = int(round(window_seconds * 1000))
        self._clock = clock
        self._window_start: Optional[int] = None
        self._count = 0

    def _now(self, now_ms: Optional[int]) -> int:
        if now_ms is not None:
            return int(now_ms)
        if self._clock is None:
            raise ValueError("now_ms required (no clock injected)")
        return self._clock.now_ms()

    def _roll(self, now: int) -> None:
        if self._window_start is None or now >= self._window_start + self.window_ms:
            self._window_start = now
            self._count = 0

    def try_acquire(self, now_ms: Optional[int] = None) -> bool:
        now = self._now(now_ms)
        self._roll(now)
        if self._count < self.limit:
            self._count += 1
            return True
        return False

    def remaining_quota(self, now_ms: Optional[int] = None) -> int:
        now = self._now(now_ms)
        self._roll(now)
        return self.limit - self._count

    def expected_remaining_ms(self, now_ms: Optional[int] = None) -> int:
        """Ms until the current window resets (0 if quota is available)."""
        now = self._now(now_ms)
        self._roll(now)
        if self._count < self.limit:
            return 0
        return max(0, self._window_start + self.window_ms - now)

    def reset_at_ms(self, now_ms: Optional[int] = None) -> int:
        now = self._now(now_ms)
        self._roll(now)
        return self._window_start + self.window_ms


class JitterSuggester:
    """Deterministic non-negative jitter (full jitter, capped).

    Reproducible: same seed -> same sequence. Jitter is always >= 0, so
    retry_after = remaining + jitter can never be earlier than remaining.
    """

    def __init__(self, seed: int, max_jitter_ms: int = 1000):
        if max_jitter_ms < 0:
            raise ValueError("max_jitter_ms must be >= 0")
        self.max_jitter_ms = int(max_jitter_ms)
        self._rng = random.Random(seed)

    def next_ms(self, remaining_ms: int) -> int:
        cap = min(self.max_jitter_ms, max(0, int(remaining_ms)))
        if cap <= 0:
            return 0
        return int(self._rng.uniform(0, cap))


@dataclass(frozen=True)
class RetryHint:
    allowed: bool
    kind: str
    retry_after_ms: int
    remaining_ms: int
    jitter_ms: int
    quota_remaining: int
    quota_limit: int
    reset_at_ms: int

    def format(self) -> str:
        if self.allowed:
            return (
                f"ALLOW kind={self.kind} "
                f"quota={self.quota_remaining}/{self.quota_limit} "
                f"reset_in={self.remaining_ms}ms"
            )
        return (
            f"RETRY kind={self.kind} after={self.retry_after_ms}ms "
            f"remaining={self.remaining_ms}ms jitter={self.jitter_ms}ms "
            f"quota={self.quota_remaining}/{self.quota_limit} "
            f"reset_at={self.reset_at_ms}"
        )


def safe_retry_after_ms(remaining_ms: int, jitter_ms: int) -> int:
    """Floor rule: never return less than the actual remaining time."""
    return max(0, int(remaining_ms)) + max(0, int(jitter_ms))


def make_hint(
    limiter,
    allowed: bool,
    jitterer: Optional[JitterSuggester] = None,
    now_ms: Optional[int] = None,
) -> RetryHint:
    """Build a RetryHint from the limiter's own counters (single source)."""
    remaining = limiter.expected_remaining_ms(now_ms)
    quota = limiter.remaining_quota(now_ms)
    reset_at = limiter.reset_at_ms(now_ms)
    limit = (
        int(limiter.capacity)
        if isinstance(limiter, PerSecondLimiter)
        else limiter.limit
    )
    jitter = 0
    if not allowed and jitterer is not None:
        jitter = jitterer.next_ms(remaining)
    return RetryHint(
        allowed=allowed,
        kind=limiter.kind,
        retry_after_ms=safe_retry_after_ms(remaining, jitter),
        remaining_ms=remaining,
        jitter_ms=jitter,
        quota_remaining=quota,
        quota_limit=limit,
        reset_at_ms=reset_at,
    )


def validate_hint(hint: RetryHint, limiter, now_ms: Optional[int] = None) -> List[str]:
    """Consistency check of a hint against the limiter's internal counters.

    Returns a list of violations (empty == consistent). Guard against a
    miscomputed hint sending every client back at the same time.
    """
    problems: List[str] = []
    actual_remaining = limiter.expected_remaining_ms(now_ms)
    actual_quota = limiter.remaining_quota(now_ms)
    actual_reset = limiter.reset_at_ms(now_ms)
    if hint.remaining_ms != actual_remaining:
        problems.append(
            f"remaining_ms: hint={hint.remaining_ms} limiter={actual_remaining}"
        )
    if hint.quota_remaining != actual_quota:
        problems.append(
            f"quota_remaining: hint={hint.quota_remaining} limiter={actual_quota}"
        )
    if hint.reset_at_ms != actual_reset:
        problems.append(
            f"reset_at_ms: hint={hint.reset_at_ms} limiter={actual_reset}"
        )
    if hint.jitter_ms < 0:
        problems.append(f"jitter_ms negative: {hint.jitter_ms}")
    if hint.retry_after_ms < actual_remaining:
        problems.append(
            f"retry_after_ms {hint.retry_after_ms} < actual remaining "
            f"{actual_remaining} (client would retry too early)"
        )
    return problems


def enforce_floor(hint: RetryHint, limiter, now_ms: Optional[int] = None) -> RetryHint:
    """Return a corrected hint if it violates the floor rule, else the hint."""
    actual_remaining = limiter.expected_remaining_ms(now_ms)
    if hint.retry_after_ms >= actual_remaining and hint.jitter_ms >= 0:
        return hint
    jitter = max(0, hint.jitter_ms)
    return replace(
        hint,
        remaining_ms=actual_remaining,
        jitter_ms=jitter,
        retry_after_ms=safe_retry_after_ms(actual_remaining, jitter),
    )

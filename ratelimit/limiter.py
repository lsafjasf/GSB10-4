"""Fixed-window rate limiter with an injectable clock.

Window model (anchored fixed window):
    window_index(now) = floor((now - epoch) / window_seconds)
    window_start      = epoch + window_index * window_seconds
    window_end        = window_start + window_seconds
    remaining_raw     = window_end - now            (seconds, float)
    remaining_ms      = ceil(remaining_raw * 1000)  (never under the raw value)

Counter: `used` counts consumed quota inside the *current* window index.
When the clock moves into a newer window, the counter resets.

Clock rewind policy ("observe", default): if now() goes backwards, the
limiter keeps the previously observed window and count. Remaining time is
computed against the *last observed* (maximum) timestamp, so the reported
remaining window can only grow, never shrink, under a rewound clock --
clients are never told to retry earlier than the server-side window allows.
Policy "raise" throws ClockRewound instead.

`kind` is only a label for the hint format ("second" / "window"); the math
is identical, which keeps per-second and per-window rules consistent.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .clock import Clock, as_clock

KIND_SECOND = "second"
KIND_WINDOW = "window"


class ClockRewound(RuntimeError):
    """Raised in strict rewind policy when the injected clock goes backwards."""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    used: int
    remaining: int
    window_index: int
    window_start: float
    window_end: float
    reset_at: float          # == window_end, absolute epoch seconds
    remaining_raw: float     # exact float seconds left in the window
    remaining_ms: int        # ceil(remaining_raw * 1000), never < raw
    now: float
    kind: str
    clock_skew_count: int


class FixedWindowLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock=None,
        kind: str = KIND_WINDOW,
        epoch: float = 0.0,
        rewind_policy: str = "observe",
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if kind not in (KIND_SECOND, KIND_WINDOW):
            raise ValueError("kind must be 'second' or 'window'")
        if rewind_policy not in ("observe", "raise"):
            raise ValueError("rewind_policy must be 'observe' or 'raise'")
        self.limit = int(limit)
        self.window_seconds = float(window_seconds)
        self.clock: Clock = as_clock(clock if clock is not None else 0.0)
        self.kind = kind
        self.epoch = float(epoch)
        self.rewind_policy = rewind_policy

        self._window_index: Optional[int] = None
        self._used = 0
        self._last_now: Optional[float] = None
        self.clock_skew_count = 0

    # -- window math -----------------------------------------------------
    def window_index_of(self, t: float) -> int:
        return math.floor((t - self.epoch) / self.window_seconds)

    def window_start_of(self, index: int) -> float:
        return self.epoch + index * self.window_seconds

    # -- main entry ------------------------------------------------------
    def acquire(self, cost: int = 1, now: Optional[float] = None) -> Decision:
        if cost <= 0:
            raise ValueError("cost must be positive")
        t = float(now) if now is not None else self.clock.now()

        if self._last_now is not None and t < self._last_now:
            self.clock_skew_count += 1
            if self.rewind_policy == "raise":
                raise ClockRewound(
                    f"clock rewound: {t!r} < last observed {self._last_now!r}"
                )
            # "observe": evaluate against the last observed (max) timestamp
            t_eval = self._last_now
        else:
            t_eval = t
        self._last_now = t_eval if self._last_now is None else max(self._last_now, t_eval)

        index = self.window_index_of(t_eval)
        if self._window_index is None or index != self._window_index:
            self._window_index = index
            self._used = 0

        allowed = self._used + cost <= self.limit
        if allowed:
            self._used += cost

        start = self.window_start_of(index)
        end = start + self.window_seconds
        remaining_raw = max(0.0, end - t_eval)
        return Decision(
            allowed=allowed,
            limit=self.limit,
            used=self._used,
            remaining=max(0, self.limit - self._used),
            window_index=index,
            window_start=start,
            window_end=end,
            reset_at=end,
            remaining_raw=remaining_raw,
            remaining_ms=math.ceil(remaining_raw * 1000.0),
            now=t_eval,
            kind=self.kind,
            clock_skew_count=self.clock_skew_count,
        )

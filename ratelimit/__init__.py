"""Rate-limit responses and retry hints (stdlib only, injectable clock)."""
from .clock import Clock, ManualClock, ScriptClock, SystemClock, as_clock
from .hints import (
    DEFAULT_SEED,
    DEFAULT_SPREAD_MS,
    HintFloorViolation,
    JitterSuggester,
    JitterSuggestion,
    base_delay_ms,
    floor_ms,
    guard_hint,
)
from .limiter import (
    KIND_SECOND,
    KIND_WINDOW,
    ClockRewound,
    Decision,
    FixedWindowLimiter,
)
from .response import RateLimitResponse, ResponseBuilder
from .rng import SplitMix64

__all__ = [
    "Clock", "ManualClock", "ScriptClock", "SystemClock", "as_clock",
    "DEFAULT_SEED", "DEFAULT_SPREAD_MS", "HintFloorViolation",
    "JitterSuggester", "JitterSuggestion", "base_delay_ms", "floor_ms",
    "guard_hint",
    "KIND_SECOND", "KIND_WINDOW", "ClockRewound", "Decision",
    "FixedWindowLimiter",
    "RateLimitResponse", "ResponseBuilder",
    "SplitMix64",
]

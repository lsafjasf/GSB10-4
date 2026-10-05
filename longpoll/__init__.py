"""长轮询超时与重连退避（标准库实现，时间/抖动可注入）。"""

from .reconnect import (
    Clock,
    ClockMovedBackwards,
    IllegalTransition,
    Intent,
    Jitter,
    MonotonicClock,
    ReconnectError,
    ReconnectPolicy,
    Reconnector,
    RetryAfter,
    SeedJitter,
    State,
    VirtualClock,
)

__all__ = [
    "Clock",
    "ClockMovedBackwards",
    "IllegalTransition",
    "Intent",
    "Jitter",
    "MonotonicClock",
    "ReconnectError",
    "ReconnectPolicy",
    "Reconnector",
    "RetryAfter",
    "SeedJitter",
    "State",
    "VirtualClock",
]

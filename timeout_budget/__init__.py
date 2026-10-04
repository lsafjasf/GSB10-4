"""Request-chain timeout budget splitting (stdlib only, injectable clock).

Public surface:
    Budget, Stage, CallContext, ChainResult, Event
    Outcome, EventType, BudgetError, BudgetExhausted, StageError, AbortError, SliceTimeout
    run, as_monotonic, MonotonicClock, VirtualClock, RealMonotonicClock
"""

from .clock import (
    Clock,
    VirtualClock,
    MonotonicClock,
    RealMonotonicClock,
    as_monotonic,
)
from .budget import (
    Budget,
    BudgetError,
    BudgetExhausted,
)
from .chain import (
    Stage,
    CallContext,
    Event,
    EventType,
    ChainResult,
    Outcome,
    StageError,
    AbortError,
    SliceTimeout,
    run,
)

__all__ = [
    "Budget",
    "Stage",
    "CallContext",
    "Event",
    "EventType",
    "ChainResult",
    "Outcome",
    "BudgetError",
    "BudgetExhausted",
    "StageError",
    "AbortError",
    "SliceTimeout",
    "run",
    "as_monotonic",
    "Clock",
    "VirtualClock",
    "MonotonicClock",
    "RealMonotonicClock",
]

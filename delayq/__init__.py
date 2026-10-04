"""DelayQ: a persistent delayed-message queue (Python 3 standard library only).

See delayq/store.py for the full semantics and docs/SEQUENCING.md for the
ordering / persistence rules.
"""

from .store import (
    DelayedQueue,
    CancelResult,
    NOT_FOUND,
    CANCELLED,
    ALREADY_DELIVERED,
    DeliveryRecord,
)

__all__ = [
    "DelayedQueue",
    "CancelResult",
    "NOT_FOUND",
    "CANCELLED",
    "ALREADY_DELIVERED",
    "DeliveryRecord",
]

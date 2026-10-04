"""Dead-letter classification, bounded retry and requeue library (stdlib only)."""

from .dlq import (
    Category,
    Clock,
    DeadLetter,
    FailureRecord,
    Message,
    Phase,
    RetryPolicy,
    classify,
    DeadLetterProcessor,
)

__all__ = [
    "Category",
    "Clock",
    "DeadLetter",
    "FailureRecord",
    "Message",
    "Phase",
    "RetryPolicy",
    "classify",
    "DeadLetterProcessor",
]

"""Failure reason classification: retryable vs non-retryable.

Unknown codes default to NON_RETRYABLE (fail-closed): an unrecognized error
must not loop forever consuming resources; it goes straight to the DLQ where an
operator can inspect it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FailureClass(str, Enum):
    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"


# Transient errors that typically clear themselves: transport hiccups, server
# side overload, temporary locks.
RETRYABLE_CODES = frozenset(
    {
        "NETWORK_TIMEOUT",
        "CONNECTION_RESET",
        "CONNECTION_REFUSED",
        "DNS_FAILURE",
        "HTTP_408",  # request timeout
        "HTTP_429",  # rate limited
        "HTTP_500",
        "HTTP_502",
        "HTTP_503",
        "HTTP_504",
        "THROTTLED",
        "TEMPORARILY_UNAVAILABLE",
        "LOCK_CONTENTION",
        "RESOURCE_EXHAUSTED",
    }
)

# Permanent errors: retrying cannot change the outcome.
NON_RETRYABLE_CODES = frozenset(
    {
        "VALIDATION_ERROR",
        "MALFORMED_PAYLOAD",
        "DESERIALIZATION_ERROR",
        "SCHEMA_MISMATCH",
        "MESSAGE_TOO_LARGE",
        "AUTH_FAILED",
        "PERMISSION_DENIED",
        "UNKNOWN_RECIPIENT",
        "HTTP_400",
        "HTTP_401",
        "HTTP_403",
        "HTTP_404",
        "HTTP_409",
        "HTTP_422",
    }
)


@dataclass(frozen=True)
class FailureReason:
    code: str
    detail: str = ""
    occurred_at: float = 0.0


def classify(reason: FailureReason) -> FailureClass:
    if reason.code in RETRYABLE_CODES:
        return FailureClass.RETRYABLE
    # Known permanent codes and anything unknown fail closed -> DLQ.
    return FailureClass.NON_RETRYABLE

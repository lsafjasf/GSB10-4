"""Dead-letter engine: failure classification, bounded retries, DLQ, replay.

Time and randomness are injected (clock callable, random.Random), so tests
fully control both. All state is in-memory; persistence is out of scope.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .classifier import FailureClass, FailureReason, classify
from .policy import RetryPolicy


class Status(str, Enum):
    PENDING = "pending"  # never delivered yet
    IN_FLIGHT = "in_flight"  # delivered, waiting for outcome
    RETRYING = "retrying"  # failed, waiting for scheduled retry
    SUCCEEDED = "succeeded"
    DEAD = "dead"  # in the DLQ


@dataclass(frozen=True)
class FailureRecord:
    attempt: int
    reason: FailureReason
    klass: FailureClass


@dataclass
class MessageRecord:
    message_id: str
    queue: str
    payload: bytes
    headers: dict
    enqueued_at: float
    attempts: int = 0
    failures: list = field(default_factory=list)  # list[FailureRecord]
    status: Status = Status.PENDING
    next_delivery_at: Optional[float] = None
    replay_count: int = 0


@dataclass
class DeadLetterRecord:
    """Immutable snapshot of the original message plus full failure history."""

    message_id: str
    queue: str
    payload: bytes
    headers: dict
    enqueued_at: float
    dead_at: float
    attempts: int
    failures: tuple  # tuple[FailureRecord, ...]
    replayed: int = 0


class FailureOutcome(Enum):
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    DUPLICATE_IGNORED = "duplicate_ignored"


class Processor:
    def __init__(
        self,
        clock: Callable[[], float],
        rng: Optional[random.Random] = None,
        policy: Optional[RetryPolicy] = None,
    ):
        self._clock = clock
        self._rng = rng if rng is not None else random.Random(0)
        self._policy = policy or RetryPolicy()
        self._records: dict[str, MessageRecord] = {}
        self._dlq: dict[str, DeadLetterRecord] = {}
        # Idempotency keys of failure reports already applied. Scoped by
        # (message_id, replay_count, attempt) so a replay starts a fresh
        # lifecycle without colliding with the previous one.
        self._applied_failures: set[tuple[str, int, int]] = set()

    # -- intake -----------------------------------------------------------

    def submit(
        self,
        message_id: str,
        queue: str,
        payload: bytes,
        headers: Optional[dict] = None,
    ) -> MessageRecord:
        """Enqueue a message. Re-submitting an existing id is a no-op (dedupe)."""
        rec = self._records.get(message_id)
        if rec is not None:
            return rec
        rec = MessageRecord(
            message_id=message_id,
            queue=queue,
            payload=payload,
            headers=dict(headers or {}),
            enqueued_at=self._clock(),
        )
        self._records[message_id] = rec
        return rec

    # -- delivery ---------------------------------------------------------

    def begin_delivery(self, message_id: str) -> int:
        """Start a delivery attempt; returns the 1-based attempt number.

        Duplicate delivery of an in-flight message (at-least-once) returns the
        current attempt number without incrementing it.
        """
        rec = self._records[message_id]
        if rec.status is Status.IN_FLIGHT:
            return rec.attempts
        if rec.status not in (Status.PENDING, Status.RETRYING):
            raise RuntimeError(f"message {message_id} not deliverable: {rec.status}")
        rec.attempts += 1
        rec.status = Status.IN_FLIGHT
        rec.next_delivery_at = None
        return rec.attempts

    def on_success(self, message_id: str) -> None:
        rec = self._records[message_id]
        if rec.status is Status.SUCCEEDED:
            return  # duplicate success report
        if rec.status is not Status.IN_FLIGHT:
            raise RuntimeError(f"message {message_id} not in flight: {rec.status}")
        rec.status = Status.SUCCEEDED

    def on_failure(self, message_id: str, reason: FailureReason) -> FailureOutcome:
        rec = self._records[message_id]
        key = (message_id, rec.replay_count, rec.attempts)
        if rec.status is not Status.IN_FLIGHT:
            # A duplicate report for an attempt whose outcome was already
            # applied (message moved on to RETRYING/DEAD) is ignored, not an
            # error -- at-least-once transports do this routinely.
            if key in self._applied_failures:
                return FailureOutcome.DUPLICATE_IGNORED
            raise RuntimeError(f"message {message_id} not in flight: {rec.status}")
        if key in self._applied_failures:
            return FailureOutcome.DUPLICATE_IGNORED
        self._applied_failures.add(key)

        klass = classify(reason)
        rec.failures.append(FailureRecord(rec.attempts, reason, klass))

        retryable = (
            klass is FailureClass.RETRYABLE and rec.attempts < self._policy.max_attempts
        )
        if retryable:
            rec.status = Status.RETRYING
            rec.next_delivery_at = self._clock() + self._policy.delay(
                rec.attempts, self._rng
            )
            return FailureOutcome.RETRY_SCHEDULED

        rec.status = Status.DEAD
        self._dlq[message_id] = DeadLetterRecord(
            message_id=rec.message_id,
            queue=rec.queue,
            payload=rec.payload,
            headers=dict(rec.headers),
            enqueued_at=rec.enqueued_at,
            dead_at=self._clock(),
            attempts=rec.attempts,
            failures=tuple(rec.failures),
            replayed=rec.replay_count,
        )
        return FailureOutcome.DEAD_LETTERED

    # -- retry scheduling ---------------------------------------------------

    def due(self, now: Optional[float] = None) -> list[str]:
        """Ids of messages whose retry is due at `now` (default: injected clock)."""
        t = self._clock() if now is None else now
        return [
            rec.message_id
            for rec in self._records.values()
            if rec.status is Status.RETRYING
            and rec.next_delivery_at is not None
            and rec.next_delivery_at <= t
        ]

    # -- dead letters ---------------------------------------------------------

    def dead_letter(self, message_id: str) -> DeadLetterRecord:
        return self._dlq[message_id]

    @property
    def dead_letters(self) -> dict[str, DeadLetterRecord]:
        return dict(self._dlq)

    def replay(self, message_id: str) -> MessageRecord:
        """Requeue a dead-lettered message onto its original queue.

        The DLQ entry keeps the original payload and the full failure history;
        the live record starts a fresh attempt lifecycle.
        """
        entry = self._dlq.get(message_id)
        if entry is None:
            raise KeyError(f"{message_id} is not dead-lettered")
        rec = self._records[message_id]
        rec.status = Status.PENDING
        rec.attempts = 0
        rec.next_delivery_at = None
        rec.replay_count += 1
        entry.replayed += 1
        return rec

    # -- introspection ------------------------------------------------------

    def record(self, message_id: str) -> MessageRecord:
        return self._records[message_id]

    @property
    def policy(self) -> RetryPolicy:
        return self._policy

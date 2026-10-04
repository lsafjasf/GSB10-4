"""Dead-letter classification, bounded retry and requeue library.

Standard library only. All wall-clock time is injected through ``Clock`` so
tests and benchmarks fully control the schedule.

Design rules (see RULES.md for the full spec):

* Failure reasons are classified into retryable vs non-retryable *before* any
  counter decision, so a non-retryable failure can never burn retry budget and
  a retryable failure can never be dead-lettered early.
* Retryable failures get at most ``RetryPolicy.max_attempts`` delivery
  attempts (default 5). Non-retryable failures get exactly 1 attempt and go
  straight to the dead-letter table.
* Retry delay is exponential backoff with *full jitter*:
  ``delay = U(0, min(cap, base * 2**(attempt-1)))``. The jitter RNG is seeded
  per message (``seed | message_id``), so the retry schedule of a message is
  reproducible and independent of processing order.
* Counters are bounded per message: ``attempts <= max_attempts``,
  ``requeues <= max_requeues`` and ``len(failures) <= attempts + requeues``.
  Duplicate failure reports for the same attempt are idempotent; duplicate
  deliveries of the same message id are rejected.
* Dead-letter entries keep the original message plus every failure record,
  and can be requeued back to their origin queue.
"""

from __future__ import annotations

import heapq
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------
# Failure classification
# --------------------------------------------------------------------------

class Category(Enum):
    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"


class ErrorCode:
    """Built-in failure reasons. Applications may use any string code and
    override its category via ``RetryPolicy.category_overrides``."""

    # Retryable: the failure is transient, a later attempt may succeed.
    TIMEOUT = "timeout"
    CONNECTION_RESET = "connection_reset"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    INTERNAL = "internal"

    # Non-retryable: retrying the identical message can never succeed.
    VALIDATION = "validation"
    DESERIALIZATION = "deserialization"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"  # unclassified error, treated as poison


_DEFAULT_CATEGORY: Dict[str, Category] = {
    ErrorCode.TIMEOUT: Category.RETRYABLE,
    ErrorCode.CONNECTION_RESET: Category.RETRYABLE,
    ErrorCode.UNAVAILABLE: Category.RETRYABLE,
    ErrorCode.RATE_LIMITED: Category.RETRYABLE,
    ErrorCode.INTERNAL: Category.RETRYABLE,
    ErrorCode.VALIDATION: Category.NON_RETRYABLE,
    ErrorCode.DESERIALIZATION: Category.NON_RETRYABLE,
    ErrorCode.AUTH: Category.NON_RETRYABLE,
    ErrorCode.NOT_FOUND: Category.NON_RETRYABLE,
    ErrorCode.UNKNOWN: Category.NON_RETRYABLE,
}


def classify(code: str, overrides: Optional[Dict[str, Category]] = None) -> Category:
    """Map a failure reason code to its category.

    Unknown codes default to NON_RETRYABLE (fail-safe): an unrecognised
    failure is treated as poison so it can never loop forever. Use
    ``overrides`` to reclassify application-specific codes.
    """
    if overrides and code in overrides:
        return overrides[code]
    return _DEFAULT_CATEGORY.get(code, Category.NON_RETRYABLE)


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5          # delivery attempts for retryable failures
    base_delay: float = 1.0        # seconds, delay for the first retry
    multiplier: float = 2.0        # exponential factor
    max_delay: float = 60.0        # cap on the pre-jitter delay
    jitter: bool = True            # full jitter: U(0, capped_delay)
    max_requeues: int = 1          # how many times a dead letter may be requeued
    seed: int = 0                  # base seed for the per-message jitter RNG
    category_overrides: Optional[Dict[str, Category]] = None


# --------------------------------------------------------------------------
# Time (injected)
# --------------------------------------------------------------------------

class Clock:
    """Wall clock wrapper. Tests drive it manually; production uses real time."""

    def __init__(self, now: float = 0.0, real: bool = False):
        self._now = float(now)
        self._real = real

    def now(self) -> float:
        return time.time() if self._real else self._now

    def set(self, now: float) -> None:
        self._now = float(now)

    def advance(self, seconds: float) -> None:
        self._now += seconds


# --------------------------------------------------------------------------
# Messages and records
# --------------------------------------------------------------------------

@dataclass
class Message:
    id: str
    queue: str
    payload: bytes
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class FailureRecord:
    attempt: int
    code: str
    category: Category
    detail: str
    failed_at: float


class Phase(Enum):
    PENDING = "pending"          # waiting for its next delivery attempt
    IN_FLIGHT = "in_flight"      # handed out, waiting for ack/nack
    SUCCEEDED = "succeeded"      # terminally done
    DEAD = "dead"                # in the dead-letter table


@dataclass
class _State:
    message: Message
    phase: Phase = Phase.PENDING
    attempts: int = 0
    requeues: int = 0
    failures: List[FailureRecord] = field(default_factory=list)
    next_attempt_at: float = 0.0
    # (attempt, code) pairs already recorded, for idempotent failure reports
    _seen_failures: set = field(default_factory=set)


@dataclass
class DeadLetter:
    """Public read-only view of a dead-letter entry."""

    message: Message
    failures: Tuple[FailureRecord, ...]
    attempts: int
    requeues: int
    dead_at: float
    last_code: str
    last_category: Category


# --------------------------------------------------------------------------
# Processor
# --------------------------------------------------------------------------

class DeadLetterProcessor:
    """Owns per-message state, the retry schedule and the dead-letter table."""

    def __init__(self, policy: Optional[RetryPolicy] = None, clock: Optional[Clock] = None):
        self.policy = policy or RetryPolicy()
        self.clock = clock or Clock(real=True)
        self._states: Dict[str, _State] = {}
        self._dead: Dict[str, _State] = {}
        # min-heap of (next_attempt_at, seq, message_id); stale entries are
        # skipped lazily so the heap stays O(number of live messages).
        self._heap: List[Tuple[float, int, str]] = []
        self._seq = 0

    # -- intake ----------------------------------------------------------

    def enqueue(self, message: Message) -> bool:
        """Register a message for delivery. Returns False for a duplicate id
        (duplicate delivery must not reset counters or duplicate state)."""
        if message.id in self._states or message.id in self._dead:
            return False
        state = _State(message=message, next_attempt_at=self.clock.now())
        self._states[message.id] = state
        self._push(state)
        return True

    # -- delivery lifecycle ----------------------------------------------

    def due_ids(self, now: Optional[float] = None, limit: Optional[int] = None) -> List[str]:
        """Ids of pending messages whose next attempt time has arrived."""
        now = self.clock.now() if now is None else now
        out: List[str] = []
        skipped: List[Tuple[float, int, str]] = []
        while self._heap and self._heap[0][0] <= now and (limit is None or len(out) < limit):
            _, _, mid = heapq.heappop(self._heap)
            state = self._states.get(mid)
            if state is None or state.phase is not Phase.PENDING:
                continue  # stale heap entry
            if state.next_attempt_at > now:
                skipped.append((state.next_attempt_at, self._seq, mid))
                self._seq += 1
                continue
            state.phase = Phase.IN_FLIGHT
            state.attempts += 1
            out.append(mid)
        for entry in skipped:
            heapq.heappush(self._heap, entry)
        return out

    def ack(self, message_id: str) -> bool:
        """Report success for the in-flight attempt."""
        state = self._states.get(message_id)
        if state is None or state.phase is not Phase.IN_FLIGHT:
            return False
        state.phase = Phase.SUCCEEDED
        return True

    def fail(self, message_id: str, code: str, detail: str = "") -> Category:
        """Report failure for the in-flight attempt.

        Idempotent per (attempt, code): reporting the same failure twice does
        not grow counters or the failure log. Returns the failure category.
        """
        category = classify(code, self.policy.category_overrides)
        key = (None, code)
        state = self._states.get(message_id)
        if state is None:
            # Already dead: only an exact duplicate of the terminal report is
            # accepted; counters never move after death.
            dead = self._dead.get(message_id)
            if dead is None:
                raise KeyError(f"unknown message {message_id!r}")
            if (dead.attempts, code) in dead._seen_failures:
                return category
            raise KeyError(f"message {message_id!r} is already dead-lettered")
        key = (state.attempts, code)
        if state.phase is Phase.PENDING:
            # Duplicate nack of the just-failed attempt: idempotent no-op.
            if key in state._seen_failures and state.failures[-1].attempt == state.attempts:
                return category
            raise KeyError(f"no in-flight attempt for message {message_id!r}")
        if state.phase is not Phase.IN_FLIGHT:
            raise KeyError(f"no in-flight attempt for message {message_id!r}")
        if key in state._seen_failures:
            return category  # duplicate report: no state change
        state._seen_failures.add(key)
        state.failures.append(
            FailureRecord(
                attempt=state.attempts,
                code=code,
                category=category,
                detail=detail,
                failed_at=self.clock.now(),
            )
        )
        if category is Category.NON_RETRYABLE or state.attempts >= self.policy.max_attempts:
            self._to_dead(state)
        else:
            state.phase = Phase.PENDING
            state.next_attempt_at = self.clock.now() + self._delay(state.attempts, message_id)
            self._push(state)
        return category

    # -- dead-letter table -------------------------------------------------

    def dead_letters(self) -> List[DeadLetter]:
        return [self._view(s) for s in self._dead.values()]

    def dead_count(self) -> int:
        return len(self._dead)

    def get_dead(self, message_id: str) -> Optional[DeadLetter]:
        state = self._dead.get(message_id)
        return self._view(state) if state else None

    def requeue(self, message_id: str) -> bool:
        """Move a dead letter back to its origin queue for a fresh retry cycle.

        The original message and its full failure history are preserved;
        ``attempts`` resets but ``requeues`` is bounded by
        ``RetryPolicy.max_requeues`` so total work per message stays bounded.
        """
        state = self._dead.pop(message_id, None)
        if state is None:
            return False
        if state.requeues >= self.policy.max_requeues:
            self._dead[message_id] = state  # put it back, stay dead
            return False
        state.requeues += 1
        state.attempts = 0
        state._seen_failures.clear()  # attempt numbering restarts per cycle
        state.phase = Phase.PENDING
        state.next_attempt_at = self.clock.now()
        self._states[message_id] = state
        self._push(state)
        return True

    # -- introspection -----------------------------------------------------

    def attempts(self, message_id: str) -> int:
        state = self._states.get(message_id) or self._dead.get(message_id)
        return state.attempts if state else 0

    def failure_count(self, message_id: str) -> int:
        state = self._states.get(message_id) or self._dead.get(message_id)
        return len(state.failures) if state else 0

    def phase(self, message_id: str) -> Optional[Phase]:
        state = self._states.get(message_id) or self._dead.get(message_id)
        return state.phase if state else None

    def next_attempt_at(self, message_id: str) -> Optional[float]:
        state = self._states.get(message_id)
        return state.next_attempt_at if state else None

    # -- internals -----------------------------------------------------------

    def _delay(self, attempt: int, message_id: str) -> float:
        """Backoff for the retry that follows attempt ``attempt`` (1-based)."""
        p = self.policy
        capped = min(p.max_delay, p.base_delay * (p.multiplier ** (attempt - 1)))
        if not p.jitter:
            return capped
        # Per-message RNG: reproducible regardless of processing order.
        rng = random.Random(f"{p.seed}|{message_id}|{attempt}")
        return rng.uniform(0.0, capped)

    def _push(self, state: _State) -> None:
        heapq.heappush(self._heap, (state.next_attempt_at, self._seq, state.message.id))
        self._seq += 1

    def _to_dead(self, state: _State) -> None:
        state.phase = Phase.DEAD
        self._states.pop(state.message.id, None)
        self._dead[state.message.id] = state

    @staticmethod
    def _view(state: _State) -> DeadLetter:
        return DeadLetter(
            message=state.message,
            failures=tuple(state.failures),
            attempts=state.attempts,
            requeues=state.requeues,
            dead_at=state.failures[-1].failed_at if state.failures else 0.0,
            last_code=state.failures[-1].code if state.failures else "",
            last_category=state.failures[-1].category if state.failures else Category.NON_RETRYABLE,
        )

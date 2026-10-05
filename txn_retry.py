"""Transaction retry with deadlock-classification, jittered backoff, shared budget.

Python 3 stdlib only. Time is fully injectable: pass `clock` / `sleep` / `rng`
so tests and benchmarks are deterministic and do not wait on the wall clock.

Backoff: exponential cap + FULL jitter (sleep = rng.uniform(0, cap)), so many
transactions hitting the same conflict do not wake in lockstep.

Budget: a single wall-time budget is shared across every retry of one
transaction. The check is "elapsed + next_delay > budget" -> terminal state,
which guarantees the scheduled total time can never exceed the budget.
"""

from __future__ import annotations

import enum
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


# --------------------------------------------------------------------------
# 1. Conflict classification
# --------------------------------------------------------------------------

class ConflictKind(enum.Enum):
    # --- retryable: transient contention, the transaction may succeed later
    DEADLOCK = "deadlock"
    LOCK_WAIT_TIMEOUT = "lock_wait_timeout"
    SERIALIZATION_FAILURE = "serialization_failure"

    # --- fatal: replaying the exact same txn fails the same way
    CONSTRAINT_VIOLATION = "constraint_violation"
    PERMISSION_DENIED = "permission_denied"
    SYNTAX_ERROR = "syntax_error"


RETRYABLE_KINDS = frozenset(
    {
        ConflictKind.DEADLOCK,
        ConflictKind.LOCK_WAIT_TIMEOUT,
        ConflictKind.SERIALIZATION_FAILURE,
    }
)


# Examples of the database/scheduler signals mapped onto each kind.
CONFLICT_EXAMPLES = {
    ConflictKind.DEADLOCK: [
        "MySQL 1213 Deadlock found when trying to get lock",
        "PostgreSQL 40P01 deadlock detected",
        "ORA-00060: deadlock detected while waiting for resource",
    ],
    ConflictKind.LOCK_WAIT_TIMEOUT: [
        "MySQL 1205 Lock wait timeout exceeded",
        "PostgreSQL 55P03 lock_not_available",
        "InnoDB innodb_lock_wait_timeout",
    ],
    ConflictKind.SERIALIZATION_FAILURE: [
        "PostgreSQL 40001 serialization_failure",
        "SQLSTATE 40001: could not serialize access due to concurrent update",
        "SSI read/write dependency abort",
    ],
    ConflictKind.CONSTRAINT_VIOLATION: [
        "PostgreSQL 23505 unique_violation (duplicate key)",
        "MySQL 1062 Duplicate entry",
        "SQLSTATE 23503 foreign_key_violation",
    ],
    ConflictKind.PERMISSION_DENIED: [
        "PostgreSQL 42501 insufficient_privilege",
        "MySQL 1142 command denied to user",
    ],
    ConflictKind.SYNTAX_ERROR: [
        "PostgreSQL 42601 syntax_error",
        "MySQL 1064 You have an error in your SQL syntax",
    ],
}


class TxnConflict(Exception):
    """A transaction failure tagged with a classified kind."""

    def __init__(
        self,
        kind: ConflictKind,
        message: str = "",
        cause: Optional[BaseException] = None,
    ) -> None:
        self.kind = kind
        self.cause = cause
        super().__init__(message or kind.value)

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS


def is_retryable(exc: BaseException) -> bool:
    """Retry policy for errors that are not TxnConflict instances."""
    return isinstance(exc, TxnConflict) and exc.kind in RETRYABLE_KINDS


# --------------------------------------------------------------------------
# 2. Policy / terminal states / result
# --------------------------------------------------------------------------

class Status(enum.Enum):
    SUCCESS = "success"
    EXHAUSTED_ATTEMPTS = "exhausted_attempts"  # hit max_attempts, still conflicting
    EXHAUSTED_BUDGET = "exhausted_budget"      # shared time budget exhausted
    NON_RETRYABLE = "non_retryable"            # fatal error, fail fast


@dataclass(frozen=True)
class RetryPolicy:
    base_delay: float = 0.010          # 10 ms
    max_delay: float = 1.0             # cap per single sleep
    multiplier: float = 2.0
    max_attempts: int = 8              # total attempts incl. the first one
    budget: float = 5.0                # SHARED across all retries of one txn

    def raw_delay(self, attempt_idx: int) -> float:
        """Exponential cap for the sleep AFTER attempt `attempt_idx` (0-based)."""
        return min(self.max_delay, self.base_delay * self.multiplier ** attempt_idx)


@dataclass
class AttemptRecord:
    index: int            # 0-based attempt number
    started_at: float     # injected-clock time the attempt began
    kind: Optional[str]   # conflict kind value, or None on success


@dataclass
class RetryResult:
    status: Status
    value: Any = None
    attempts: List[AttemptRecord] = field(default_factory=list)
    schedule: List[float] = field(default_factory=list)  # absolute retry times
    delays: List[float] = field(default_factory=list)    # jittered sleeps taken
    last_error: Optional[TxnConflict] = None
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def elapsed(self) -> float:
        return self.finished_at - self.started_at

    @property
    def num_attempts(self) -> int:
        return len(self.attempts)

    @property
    def num_retries(self) -> int:
        return len(self.schedule)


# --------------------------------------------------------------------------
# 3. Retryer
# --------------------------------------------------------------------------

class Retryer:
    """Runs a transactional callable under the retry policy.

    clock:  zero-based monotonic time source (defaults: time.monotonic)
    sleep:  function that actually advances time (defaults: time.sleep)
    rng:    random.Random, inject random.Random(seed) for reproducible jitter
    """

    def __init__(
        self,
        policy: Optional[RetryPolicy] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.policy = policy or RetryPolicy()
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()

    def run(self, fn: Callable[[], Any]) -> RetryResult:
        p = self.policy
        started_at = self.clock()
        result = RetryResult(status=Status.SUCCESS, started_at=started_at)
        idx = 0
        while True:
            now = self.clock()
            try:
                value = fn()
            except TxnConflict as exc:
                result.attempts.append(
                    AttemptRecord(index=idx, started_at=now, kind=exc.kind.value)
                )
                result.last_error = exc

                if exc.kind not in RETRYABLE_KINDS:
                    result.status = Status.NON_RETRYABLE
                    break

                if idx + 1 >= p.max_attempts:
                    result.status = Status.EXHAUSTED_ATTEMPTS
                    break

                cap = p.raw_delay(idx)
                delay = self.rng.uniform(0.0, cap)  # full jitter
                elapsed = self.clock() - started_at
                if elapsed + delay > p.budget:
                    # Consuming this delay would overrun the shared budget:
                    # stop BEFORE sleeping, keep last_error as the cause.
                    result.status = Status.EXHAUSTED_BUDGET
                    break

                self.sleep(delay)
                result.delays.append(delay)
                result.schedule.append(self.clock())
                idx += 1
                continue
            else:
                result.value = value
                result.attempts.append(
                    AttemptRecord(index=idx, started_at=now, kind=None)
                )
                result.status = Status.SUCCESS
                break

        result.finished_at = self.clock()
        return result

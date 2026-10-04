"""Chain runner: stages share one Budget, retries share it too."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .budget import Budget, BudgetExhausted
from .clock import Clock


class Outcome(str, Enum):
    """The explicit terminal status of a chain. Never silent."""

    SUCCESS = "success"
    BUDGET_EXHAUSTED = "budget_exhausted"
    STAGE_ERROR = "stage_error"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"


class EventType(str, Enum):
    QUEUE = "queue"
    STAGE = "stage"
    BACKOFF = "backoff"


class StageError(Exception):
    """Permanent failure inside a stage. Retryable only if ``retryable``."""

    def __init__(self, message: str = "", *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)


class AbortError(StageError):
    """Convenience: a permanent, non-retryable stage failure."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message, retryable=False)


class SliceTimeout(StageError):
    """Raised by handlers (or the runtime) when a stage slice is overrun.

    Retryable by default: a timed-out attempt consumes budget and may be
    retried if the shared budget still funds another slice.
    """

    def __init__(self, message: str = "slice timeout", *, retryable: bool = True) -> None:
        super().__init__(message, retryable=retryable)


@dataclass(frozen=True)
class CallContext:
    """What a handler is allowed to see/use."""

    budget: Budget
    stage_index: int
    attempt: int  # 1-based within this stage
    slice_ticks: int
    deadline: int  # absolute tick at which this attempt must be done

    def now(self) -> int:
        return self.budget.now()


Handler = Callable[[CallContext], Any]


@dataclass(frozen=True)
class Stage:
    name: str
    weight: int = 1
    max_attempts: int = 1
    backoff_ticks: int = 0
    handler: Handler | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("stage needs a name")
        if self.weight <= 0:
            raise ValueError("stage weight must be > 0")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be > 0")
        if self.backoff_ticks < 0:
            raise ValueError("backoff_ticks must be >= 0")
        if self.handler is None:
            raise ValueError("stage needs a handler")


@dataclass
class Event:
    """One row of the budget-consumption timeline (ticks are absolute)."""

    type: EventType
    stage_index: int | None
    attempt: int | None
    name: str
    start: int
    end: int
    duration: int
    detail: str = ""


@dataclass
class ChainResult:
    outcome: Outcome
    reason: str
    value: Any = None
    error: str = ""
    total: int = 0
    used: int = 0
    remaining: int = 0
    started_at: int = 0
    finished_at: int = 0
    stages: int = 0
    attempts_total: int = 0
    events: list[Event] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.SUCCESS

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "value": self.value,
            "error": self.error,
            "total": self.total,
            "used": self.used,
            "remaining": self.remaining,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stages": self.stages,
            "attempts_total": self.attempts_total,
            "events": [
                {
                    "type": e.type.value,
                    "stage_index": e.stage_index,
                    "attempt": e.attempt,
                    "name": e.name,
                    "start": e.start,
                    "end": e.end,
                    "duration": e.duration,
                    "detail": e.detail,
                }
                for e in self.events
            ],
        }


def run(
    stages: list[Stage],
    budget: Budget,
    *,
    enforce_slice: bool = True,
) -> ChainResult:
    """Execute ``stages`` sequentially against one shared ``budget``.

    ``enforce_slice=False`` trusts handlers to stop at their deadline. The
    default ``True`` measures handler time and reports any overrun as an
    exhausted budget immediately (no silent downgrade to "no timeout").
    """
    if not stages:
        raise ValueError("run() needs at least one stage")

    clock = budget.clock
    total_weight = sum(stage.weight for stage in stages)
    started_at = clock.now()
    events: list[Event] = []

    # Queueing is whatever already elapsed since the budget was created.
    queued = started_at - budget.t0
    if queued > 0:
        events.append(
            Event(
                EventType.QUEUE,
                None,
                None,
                "queue",
                budget.t0,
                started_at,
                queued,
                "pre-chain queueing, charged to the same budget",
            )
        )

    attempts_total = 0
    remaining_weight = total_weight
    stage_index = 0
    value_to_keep = None
    try:
        while stage_index < len(stages):
            stage = stages[stage_index]
            attempt = 1
            while True:
                start = clock.now()
                slice_ticks = budget.slice_for(stage.weight, remaining_weight)
                attempts_total += 1
                ctx = CallContext(
                    budget=budget,
                    stage_index=stage_index,
                    attempt=attempt,
                    slice_ticks=slice_ticks,
                    deadline=start + slice_ticks,
                )
                try:
                    value = stage.handler(ctx)
                except SliceTimeout as exc:
                    end = clock.now()
                    events.append(
                        Event(
                            EventType.STAGE,
                            stage_index,
                            attempt,
                            stage.name,
                            start,
                            end,
                            end - start,
                            "timeout raised by handler: {}".format(exc),
                        )
                    )
                    if not exc.retryable:
                        return _finish(
                            Outcome.STAGE_ERROR,
                            "stage {!r} failed permanently: {}".format(
                                stage.name, exc
                            ),
                            budget,
                            started_at,
                            events,
                            attempts_total,
                            len(stages),
                            error=str(exc),
                        )
                    if attempt >= stage.max_attempts:
                        return _finish(
                            Outcome.ATTEMPTS_EXHAUSTED,
                            "stage {!r} used {} attempt(s); last one timed out".format(
                                stage.name, attempt
                            ),
                            budget,
                            started_at,
                            events,
                            attempts_total,
                            len(stages),
                            error=str(exc),
                        )
                    if not _retry_or_exhaust(
                        stage, attempt, budget, events, stage_index
                    ):
                        return _finish(
                            Outcome.BUDGET_EXHAUSTED,
                            "budget exhausted funding backoff/retry for "
                            "stage {!r}".format(stage.name),
                            budget,
                            started_at,
                            events,
                            attempts_total,
                            len(stages),
                        )
                    attempt += 1
                    continue
                except BudgetExhausted as exc:
                    end = clock.now()
                    events.append(
                        Event(
                            EventType.STAGE,
                            stage_index,
                            attempt,
                            stage.name,
                            start,
                            end,
                            end - start,
                            "handler signalled budget exhaustion: {}".format(
                                exc.reason
                            ),
                        )
                    )
                    return _finish(
                        Outcome.BUDGET_EXHAUSTED,
                        "stage {!r} reported budget exhaustion: {}".format(
                            stage.name, exc.reason
                        ),
                        budget,
                        started_at,
                        events,
                        attempts_total,
                        len(stages),
                    )
                except StageError as exc:
                    end = clock.now()
                    events.append(
                        Event(
                            EventType.STAGE,
                            stage_index,
                            attempt,
                            stage.name,
                            start,
                            end,
                            end - start,
                            "stage error: {}".format(exc),
                        )
                    )
                    if not exc.retryable or attempt >= stage.max_attempts:
                        return _finish(
                            Outcome.STAGE_ERROR,
                            "stage {!r} failed: {}".format(stage.name, exc),
                            budget,
                            started_at,
                            events,
                            attempts_total,
                            len(stages),
                            error=str(exc),
                        )
                    if not _retry_or_exhaust(
                        stage, attempt, budget, events, stage_index
                    ):
                        return _finish(
                            Outcome.BUDGET_EXHAUSTED,
                            "budget exhausted funding backoff/retry for "
                            "stage {!r}".format(stage.name),
                            budget,
                            started_at,
                            events,
                            attempts_total,
                            len(stages),
                        )
                    attempt += 1
                    continue

                end = clock.now()
                used_by_attempt = end - start
                events.append(
                    Event(
                        EventType.STAGE,
                        stage_index,
                        attempt,
                        stage.name,
                        start,
                        end,
                        used_by_attempt,
                        "ok" if used_by_attempt <= slice_ticks else "overran slice",
                    )
                )
                if enforce_slice and used_by_attempt > slice_ticks:
                    return _finish(
                        Outcome.BUDGET_EXHAUSTED,
                        "stage {!r} ignored its {}-tick slice and used {} "
                        "tick(s)".format(stage.name, slice_ticks, used_by_attempt),
                        budget,
                        started_at,
                        events,
                        attempts_total,
                        len(stages),
                    )
                # Success: lock this stage's weight out of future splits.
                remaining_weight -= stage.weight
                stage_index += 1
                value_to_keep = value
                break

        return _finish(
            Outcome.SUCCESS,
            "all {} stage(s) succeeded".format(len(stages)),
            budget,
            started_at,
            events,
            attempts_total,
            len(stages),
            value=value_to_keep,
        )

    except BudgetExhausted as exc:
        return _finish(
            Outcome.BUDGET_EXHAUSTED,
            "budget exhausted before dispatch: {}".format(exc.reason),
            budget,
            started_at,
            events,
            attempts_total,
            len(stages),
        )


def _retry_or_exhaust(
    stage: Stage,
    attempt: int,
    budget: Budget,
    events: list[Event],
    stage_index: int,
) -> bool:
    """Charge the backoff for the next retry to the same budget.

    Returns True if a new attempt may be scheduled (caller re-samples a
    slice), False if the budget cannot fund the backoff.
    """
    if stage.backoff_ticks <= 0:
        return True
    start = budget.now()
    try:
        budget.sleep_bounded(stage.backoff_ticks)
    except BudgetExhausted:
        return False
    end = budget.now()
    events.append(
        Event(
            EventType.BACKOFF,
            stage_index,
            attempt,
            stage.name,
            start,
            end,
            end - start,
            "retry backoff {}/{}".format(attempt, stage.max_attempts - 1),
        )
    )
    return True


def _finish(
    outcome: Outcome,
    reason: str,
    budget: Budget,
    started_at: int,
    events: list[Event],
    attempts_total: int,
    n_stages: int,
    *,
    value: Any = None,
    error: str = "",
) -> ChainResult:
    finished_at = budget.now()
    elapsed = finished_at - budget.t0
    return ChainResult(
        outcome=outcome,
        reason=reason,
        value=value,
        error=error,
        total=budget.total,
        used=min(budget.total, max(0, elapsed)),
        remaining=budget.remaining(),
        started_at=started_at,
        finished_at=finished_at,
        stages=n_stages,
        attempts_total=attempts_total,
        events=events,
    )

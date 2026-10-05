"""Shared domain model for graceful shutdown.

Both engines (simulated virtual-time and real threaded) use these exact
same primitives, so shutdown semantics are identical regardless of the
clock that drives them.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Policy(enum.Enum):
    """How a task that is cancelled mid-flight must be disposed of."""

    CHECKPOINT = "checkpoint"  # persist progress, resume later
    ROLLBACK = "rollback"  # compensate finished steps, restore pre-task state
    FAIL = "fail"  # mark failed, keep applied effects for triage


class Status(enum.Enum):
    FINISHED = "finished"  # ran to completion
    CHECKPOINTED = "checkpointed"  # interrupted; durable checkpoint written
    ROLLED_BACK = "rolled_back"  # interrupted; every applied step compensated
    FAILED = "failed"  # interrupted; marked failed, effects retained
    ABANDONED = "abandoned"  # force-exited; no durable outcome
    REJECTED = "rejected"  # arrived after intake closed; never started


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    policy: Policy
    steps: tuple[float, ...]
    stuck: bool = False
    cleanup_cost: float = 0.0
    rollback_step_cost: float = 0.0

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    def disposition_cost(self, steps_done: int) -> float:
        """Time the engine has to spend disposing of this task at a safe point."""
        if self.policy is Policy.ROLLBACK:
            return self.cleanup_cost + self.rollback_step_cost * steps_done
        return self.cleanup_cost


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    task_id: str
    op: str
    detail: str


class Ledger:
    """Write-ahead journal: an intent is appended before its effect lands.

    ``record`` is the only append point; callers journal an intent first and
    only then mutate durable state (see :func:`apply_disposition`).
    """

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []

    def record(self, task_id: str, op: str, detail: str = "") -> LedgerEntry:
        entry = LedgerEntry(len(self._entries), task_id, op, detail)
        self._entries.append(entry)
        return entry

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    def count(self, task_id: str, op: str) -> int:
        return sum(
            1 for e in self._entries if e.task_id == task_id and e.op == op
        )


@dataclass
class DurableStore:
    """Durable side effects of dispositions (what a real system would persist)."""

    checkpoints: dict[str, int] = field(default_factory=dict)
    compensations: list[tuple[str, int]] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    status: Status
    steps_done: int
    detail: str = ""


class ResultTable:
    """Final task outcomes. First write wins: a task gets exactly one outcome."""

    def __init__(self) -> None:
        self._results: dict[str, TaskResult] = {}

    def finalize(self, result: TaskResult) -> bool:
        if result.task_id in self._results:
            return False
        self._results[result.task_id] = result
        return True

    def __contains__(self, task_id: str) -> bool:
        return task_id in self._results

    def __getitem__(self, task_id: str) -> TaskResult:
        return self._results[task_id]

    def get(self, task_id: str) -> TaskResult | None:
        return self._results.get(task_id)

    def values(self):
        return self._results.values()

    def as_dict(self) -> dict[str, TaskResult]:
        return dict(self._results)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for result in self._results.values():
            out[result.status.value] = out.get(result.status.value, 0) + 1
        return out


def apply_disposition(
    spec: TaskSpec,
    steps_done: int,
    results: ResultTable,
    store: DurableStore,
    ledger: Ledger,
) -> TaskResult:
    """Commit one cancelled task's disposition.

    Order is fixed on purpose: write-ahead log -> durable state -> published
    result. The engine is responsible for having already spent the required
    cleanup time before calling this function.
    """
    tid = spec.task_id
    if spec.policy is Policy.CHECKPOINT:
        ledger.record(tid, "checkpoint", f"steps={steps_done}")
        store.checkpoints[tid] = steps_done
        result = TaskResult(
            tid,
            Status.CHECKPOINTED,
            steps_done,
            f"checkpoint at step {steps_done}/{spec.total_steps}",
        )
    elif spec.policy is Policy.ROLLBACK:
        for index in range(steps_done - 1, -1, -1):
            ledger.record(tid, "compensate", f"step={index}")
            store.compensations.append((tid, index))
        result = TaskResult(
            tid,
            Status.ROLLED_BACK,
            steps_done,
            f"compensated {steps_done} applied step(s) in reverse order",
        )
    else:
        ledger.record(tid, "mark_failed", f"steps={steps_done}")
        store.failures[tid] = steps_done
        result = TaskResult(
            tid,
            Status.FAILED,
            steps_done,
            f"marked failed; {steps_done} step(s) of effects retained for triage",
        )
    results.finalize(result)
    return result

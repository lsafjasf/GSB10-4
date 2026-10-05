"""Multi-stage task execution with partial commit and idempotent rollback.

Standard library only (Python 3).

Core model
----------
A task is an ordered list of stages. Each stage:
  * `action(ctx)`  -- does work, buffering store writes;
  * `compensate(ctx)` -- optional, used only to undo *external* side effects.

Stage-level semantics
---------------------
1. A stage is either fully committed or not committed at all. Writes issued
   inside `action` are buffered and appended to the store as one versioned
   commit only after `action` returns without raising.
2. Every committed stage is recorded in the task journal
   (state = COMMITTED, plus the exact keys it wrote and before-images).
   This journal is the single source of truth for rollback.
3. On failure, committed stages are compensated in strict reverse order.
   Store writes are undone by deleting the task's versioned writes (the
   previous version becomes visible again); user `compensate` hooks undo
   non-store side effects.
4. Compensation is journal-gated: a stage in state COMPENSATED is never
   compensated twice. Store deletion is version-tag-gated, so a second
   rollback cannot delete a re-created value that belongs to someone else.
   Re-running rollback therefore has zero additional side effects.
5. If a compensation raises, rollback STOPS at that stage (reverse order
   invariant is preserved), task state = ROLLBACK_FAILED. Re-running
   rollback retries only failed/pending stages; stages already COMPENSATED
   are skipped.
"""

from __future__ import annotations

import enum
import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Store: versioned key-value store with task-scoped visibility.
# ---------------------------------------------------------------------------

# A version is (owner_task_id_or_None, value). owner None = baseline write,
# always visible; a concrete task id is visible only after that task commits.
Version = Tuple[Optional[str], Any]


class KeyNotFound(Exception):
    pass


class Store:
    """Versioned KV store.

    Reads are *snapshot* reads with read-your-writes for the owning task:
      * reader inside task T (task_id="T"): sees T's own latest version,
        otherwise the latest baseline/committed version;
      * external reader (task_id=None): sees only baseline writes and
        versions belonging to tasks in COMMITTED state.
    """

    def __init__(self) -> None:
        self._versions: Dict[str, List[Version]] = {}
        self._committed_tasks: set[str] = set()

    # -- internal helpers used by Executor / tests -------------------------
    def _append_version(self, task_id: str, key: str, value: Any) -> None:
        self._versions.setdefault(key, []).append((task_id, value))

    def _task_committed(self, task_id: str) -> bool:
        return task_id in self._committed_tasks

    def _mark_committed(self, task_id: str) -> None:
        self._committed_tasks.add(task_id)

    def _remove_task_versions_for_keys(self, task_id: str, keys: List[str]) -> None:
        """Delete versions owned by `task_id` on the given keys.

        Tag-gated: a version owned by another task (e.g. a value created
        later by a new task) is never touched. Idempotent: removing an
        already-absent tag is a no-op.
        """
        for key in keys:
            versions = self._versions.get(key)
            if not versions:
                continue
            self._versions[key] = [v for v in versions if v[0] != task_id]
            if not self._versions[key]:
                del self._versions[key]

    # -- reads --------------------------------------------------------------
    def read(self, key: str, task_id: Optional[str] = None) -> Any:
        """External read with task_id=None; task read with its own id."""
        own_latest = None
        for owner, value in reversed(self._versions.get(key, [])):
            if task_id is not None and owner == task_id:
                if own_latest is None:
                    own_latest = value
                continue
            if owner is None or owner in self._committed_tasks:
                if own_latest is not None:
                    return own_latest
                return value
        if own_latest is not None:
            return own_latest
        raise KeyNotFound(key)

    def read_baseline(self, key: str) -> Any:
        """Write/read as if no staging existed (used by the differential test)."""
        return self.read(key, None)

    def baseline_write(self, key: str, value: Any) -> None:
        """A non-staged write: immediately and permanently visible."""
        self._versions.setdefault(key, []).append((None, value))

    def visible_snapshot(self, task_id: Optional[str] = None) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key in self._versions:
            try:
                out[key] = self.read(key, task_id)
            except KeyNotFound:
                pass
        return dict(sorted(out.items()))

    def total_version_count(self) -> int:
        return sum(len(v) for v in self._versions.values())


# ---------------------------------------------------------------------------
# Journal / task metadata
# ---------------------------------------------------------------------------

class TaskState(enum.Enum):
    RUNNING = "RUNNING"
    COMMITTED = "COMMITTED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class StageState(enum.Enum):
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    COMPENSATED = "COMPENSATED"
    COMPENSATE_FAILED = "COMPENSATE_FAILED"


@dataclass
class StageRecord:
    index: int
    name: str
    state: StageState = StageState.PENDING
    wrote_keys: List[str] = field(default_factory=list)
    before: Dict[str, Any] = field(default_factory=dict)
    had_key: Dict[str, bool] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class StageOutcome:
    index: int
    name: str
    committed: bool
    error: Optional[str] = None


@dataclass
class CompensationOutcome:
    index: int
    name: str
    state: str                      # COMPENSATED | COMPENSATE_FAILED | SKIPPED
    error: Optional[str] = None
    keys_restored: int = 0


@dataclass
class TaskResult:
    task_id: str
    state: str
    committed_stage_count: int
    failure_point: Optional[int]    # index of the stage whose action failed
    stage_outcomes: List[StageOutcome]
    rollback: List[CompensationOutcome]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "committed_stage_count": self.committed_stage_count,
            "failure_point": self.failure_point,
            "stage_outcomes": [vars(o) for o in self.stage_outcomes],
            "rollback": [vars(o) for o in self.rollback],
        }


# ---------------------------------------------------------------------------
# Stage / context / executor
# ---------------------------------------------------------------------------

class Stage:
    def __init__(
        self,
        name: str,
        action: Callable[["TaskContext"], None],
        compensate: Optional[Callable[["TaskContext"], None]] = None,
    ) -> None:
        self.name = name
        self.action = action
        self.compensate = compensate


class TaskContext:
    """Handed to actions/compensations. Writes are buffered until commit."""

    def __init__(self, task_id: str, store: Store) -> None:
        self.task_id = task_id
        self.store = store
        self._pending: Dict[str, Any] = {}

    # reads see earlier committed stages of THIS task + globally visible data
    def read(self, key: str) -> Any:
        if key in self._pending:
            return self._pending[key]
        return self.store.read(key, self.task_id)

    def try_read(self, key: str, default: Any = None) -> Any:
        try:
            return self.read(key)
        except KeyNotFound:
            return default

    def write(self, key: str, value: Any) -> None:
        self._pending[key] = value

    def _flush(self, record: StageRecord) -> None:
        for key, value in self._pending.items():
            try:
                record.before[key] = self.store.read(key, self.task_id)
                record.had_key[key] = True
            except KeyNotFound:
                record.had_key[key] = False
            record.wrote_keys.append(key)
            self.store._append_version(self.task_id, key, value)
        self._pending.clear()


class Executor:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._tasks: Dict[str, List[StageRecord]] = {}
        self._states: Dict[str, TaskState] = {}
        self._counter = itertools.count(1)

    # -- journal introspection ---------------------------------------------
    def task_state(self, task_id: str) -> TaskState:
        return self._states[task_id]

    def journal(self, task_id: str) -> List[StageRecord]:
        return self._tasks[task_id]

    def live_task_count(self) -> int:
        return len(self._tasks)

    def forget(self, task_id: str) -> None:
        """Drop the journal of a terminal task. Memory bound: see bench.py."""
        self._tasks.pop(task_id, None)
        self._states.pop(task_id, None)

    # -- main entry ---------------------------------------------------------
    def run(self, stages: List[Stage], task_id: Optional[str] = None) -> TaskResult:
        task_id = task_id or f"task-{next(self._counter)}"
        records = [StageRecord(i, s.name) for i, s in enumerate(stages)]
        self._tasks[task_id] = records
        self._states[task_id] = TaskState.RUNNING

        committed = 0
        failure_point: Optional[int] = None
        outcomes: List[StageOutcome] = []

        for i, stage in enumerate(stages):
            ctx = TaskContext(task_id, self.store)
            try:
                stage.action(ctx)
            except Exception as exc:  # stage failed -> rollback prefix
                failure_point = i
                outcomes.append(
                    StageOutcome(i, stage.name, committed=False, error=repr(exc))
                )
                break
            ctx._flush(records[i])
            records[i].state = StageState.COMMITTED
            committed += 1
            outcomes.append(StageOutcome(i, stage.name, committed=True))

        if failure_point is None:
            self.store._mark_committed(task_id)
            self._states[task_id] = TaskState.COMMITTED
            return TaskResult(
                task_id, TaskState.COMMITTED.value, committed, None, outcomes, []
            )

        rollback_outcomes = self.rollback(task_id, stages)
        return TaskResult(
            task_id,
            self._states[task_id].value,
            committed,
            failure_point,
            outcomes,
            rollback_outcomes,
        )

    # -- rollback (idempotent, resumable) -----------------------------------
    def rollback(self, task_id: str, stages: List[Stage]) -> List[CompensationOutcome]:
        """Compensate committed stages in reverse order.

        Safe to call any number of times: COMPENSATED stages are skipped and
        failed stages are retried. Stops at the first failing compensation;
        a later call resumes there.
        """
        records = self._tasks[task_id]
        if self._states[task_id] == TaskState.COMMITTED:
            raise ValueError(f"task {task_id} is COMMITTED; rollback not allowed")
        if self._states[task_id] != TaskState.ROLLBACK_FAILED:
            self._states[task_id] = TaskState.ROLLING_BACK
        outcomes: List[CompensationOutcome] = []

        for record in sorted(records, key=lambda r: r.index, reverse=True):
            if record.state == StageState.COMPENSATED:
                outcomes.append(
                    CompensationOutcome(
                        record.index, record.name, StageState.COMPENSATED.name,
                        keys_restored=0,
                    )
                )
                continue
            if record.state not in (StageState.COMMITTED, StageState.COMPENSATE_FAILED):
                continue  # PENDING: never committed, nothing to undo, not reported

            stage = stages[record.index]
            ctx = TaskContext(task_id, self.store)
            try:
                if stage.compensate is not None:
                    stage.compensate(ctx)
            except Exception as exc:
                record.state = StageState.COMPENSATE_FAILED
                record.error = repr(exc)
                outcomes.append(
                    CompensationOutcome(
                        record.index,
                        record.name,
                        StageState.COMPENSATE_FAILED.name,
                        error=repr(exc),
                    )
                )
                self._states[task_id] = TaskState.ROLLBACK_FAILED
                return outcomes

            self.store._remove_task_versions_for_keys(task_id, record.wrote_keys)
            record.state = StageState.COMPENSATED
            outcomes.append(
                CompensationOutcome(
                    record.index,
                    record.name,
                    StageState.COMPENSATED.name,
                    keys_restored=len(record.wrote_keys),
                )
            )

        self._states[task_id] = TaskState.ROLLED_BACK
        return outcomes

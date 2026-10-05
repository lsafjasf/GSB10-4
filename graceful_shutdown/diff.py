"""Diff a shutdown run against an all-completed baseline run.

Every task whose shutdown outcome differs from the baseline gets one
DiffEntry with a human-readable explanation, so the release pipeline can
account for every single divergence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .executor import ShutdownReport, TaskRecord
from .task import TaskOutcome


@dataclass
class DiffEntry:
    task_id: str
    full_outcome: TaskOutcome
    shutdown_outcome: TaskOutcome
    explanation: str


def compare_runs(
    full_records: Dict[str, TaskRecord],
    shutdown: ShutdownReport,
) -> List[DiffEntry]:
    entries: List[DiffEntry] = []
    for task_id in sorted(full_records):
        full = full_records[task_id]
        shut = shutdown.records.get(task_id)
        if shut is None:
            entries.append(
                DiffEntry(task_id, full.outcome, None, "missing from shutdown run")
            )
            continue
        if shut.outcome == full.outcome:
            continue
        entries.append(
            DiffEntry(task_id, full.outcome, shut.outcome, _explain(shut))
        )
    return entries


def _explain(shut: TaskRecord) -> str:
    outcome = shut.outcome
    if outcome is TaskOutcome.CANCELLED_BEFORE_START:
        return "never started at shutdown; no side effects, safe to rerun"
    if outcome is TaskOutcome.CHECKPOINTED:
        return (
            f"interrupted in flight; progress saved to checkpoint "
            f"{shut.checkpoint_data!r}; resume from checkpoint to reach "
            f"the full-run result"
        )
    if outcome is TaskOutcome.ROLLED_BACK:
        return "interrupted in flight; side effects reverted, safe to rerun from scratch"
    if outcome is TaskOutcome.MARKED_FAILED:
        return f"could not be disposed cleanly ({shut.detail}); needs manual intervention"
    return f"outcome differs from full run ({shut.detail})"

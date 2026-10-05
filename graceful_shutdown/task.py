"""Task contract and result vocabulary."""

from __future__ import annotations

import threading
from enum import Enum


class Disposition(str, Enum):
    """What should happen to a task that is still unfinished at the
    force-dispose phase."""

    CHECKPOINT = "checkpoint"
    ROLLBACK = "rollback"
    FAIL = "fail"


class TaskOutcome(str, Enum):
    """Final, terminal outcome recorded for every accepted task."""

    COMPLETED = "completed"
    CANCELLED_BEFORE_START = "cancelled_before_start"
    CHECKPOINTED = "checkpointed"
    ROLLED_BACK = "rolled_back"
    MARKED_FAILED = "marked_failed"


class Task:
    """Base class for work submitted to a GracefulExecutor.

    Subclasses declare their ``disposition`` and implement ``run`` plus
    the matching dispose hook (``checkpoint`` / ``rollback``). Both hooks
    must be cooperative: they get a bounded budget and are abandoned if
    they exceed it (the task is then marked failed).
    """

    disposition: Disposition = Disposition.ROLLBACK

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id

    def run(self, cancel: threading.Event) -> None:
        raise NotImplementedError

    def checkpoint(self):
        """Persist resumable state. Return value is stored as the
        checkpoint payload."""
        raise NotImplementedError

    def rollback(self) -> None:
        """Undo any side effects the task has produced."""
        raise NotImplementedError

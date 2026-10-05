"""Graceful shutdown for background task executors (stdlib only)."""

from .clock import FakeClock, SystemClock
from .diff import DiffEntry, compare_runs
from .executor import (
    GracefulExecutor,
    Phase,
    PhaseTiming,
    ShutdownInProgress,
    ShutdownReport,
    TaskRecord,
)
from .task import Disposition, Task, TaskOutcome

__all__ = [
    "DiffEntry",
    "Disposition",
    "FakeClock",
    "GracefulExecutor",
    "Phase",
    "PhaseTiming",
    "ShutdownInProgress",
    "ShutdownReport",
    "SystemClock",
    "Task",
    "TaskOutcome",
    "TaskRecord",
    "compare_runs",
]

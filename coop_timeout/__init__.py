"""Cooperative timeout / cancellation library (stdlib only, time injected)."""

from .coop import (
    CANCELLED,
    FAILED,
    OK,
    TIMEOUT,
    CancellationToken,
    CleanupReport,
    Clock,
    Observation,
    ResourceRecord,
    SystemClock,
    TaskContext,
    TaskResult,
    VirtualClock,
    run,
)

__all__ = [
    "CANCELLED",
    "FAILED",
    "OK",
    "TIMEOUT",
    "CancellationToken",
    "CleanupReport",
    "Clock",
    "Observation",
    "ResourceRecord",
    "SystemClock",
    "TaskContext",
    "TaskResult",
    "VirtualClock",
    "run",
]

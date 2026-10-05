"""Graceful shutdown for background task runners (standard library only).

Quick start::

    from graceful_shutdown import SimEngine, ShutdownConfig, run_shutdown

    engine = SimEngine(workers=8)
    for spec in specs:
        engine.submit(spec)
    report = run_shutdown(engine, ShutdownConfig(
        drain_timeout=0.1, cleanup_timeout=1.0, force_wait=0.2,
    ))
    assert report.punctual
"""
from .model import (
    DurableStore,
    Ledger,
    LedgerEntry,
    Policy,
    ResultTable,
    Status,
    TaskResult,
    TaskSpec,
    apply_disposition,
)
from .reconcile import ReconReport, TaskDiff, reconcile
from .scenarios import make_specs, run_full, run_shutdown_scenario
from .shutdown import (
    Engine,
    PhaseTiming,
    ShutdownConfig,
    ShutdownReport,
    run_shutdown,
)
from .sim import SimEngine
from .threaded import ThreadEngine

__all__ = [
    "DurableStore",
    "Engine",
    "Ledger",
    "LedgerEntry",
    "PhaseTiming",
    "Policy",
    "ReconReport",
    "ResultTable",
    "ShutdownConfig",
    "ShutdownReport",
    "SimEngine",
    "Status",
    "TaskDiff",
    "TaskResult",
    "TaskSpec",
    "ThreadEngine",
    "apply_disposition",
    "make_specs",
    "reconcile",
    "run_full",
    "run_shutdown",
    "run_shutdown_scenario",
]

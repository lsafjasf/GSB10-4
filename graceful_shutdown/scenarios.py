"""Deterministic scenario builders shared by tests and the benchmark."""
from __future__ import annotations

import random

from .model import Policy, TaskSpec
from .shutdown import ShutdownConfig, ShutdownReport, run_shutdown
from .sim import SimEngine


def make_specs(
    n: int,
    *,
    seed: int = 0,
    min_steps: int = 3,
    max_steps: int = 8,
    step_lo: float = 5e-4,
    step_hi: float = 2e-3,
    policies: tuple[Policy, ...] = (Policy.CHECKPOINT, Policy.ROLLBACK, Policy.FAIL),
    weights: tuple[float, ...] = (0.6, 0.3, 0.1),
    cleanup_lo: float = 2e-4,
    cleanup_hi: float = 1e-3,
    rollback_step_cost: float = 1e-4,
    stuck_ids: frozenset[str] = frozenset(),
) -> list[TaskSpec]:
    rng = random.Random(seed)
    specs: list[TaskSpec] = []
    for i in range(n):
        tid = f"t{i:06d}"
        policy = rng.choices(policies, weights=weights, k=1)[0]
        steps = tuple(
            rng.uniform(step_lo, step_hi)
            for _ in range(rng.randint(min_steps, max_steps))
        )
        specs.append(TaskSpec(
            task_id=tid,
            policy=policy,
            steps=steps,
            stuck=tid in stuck_ids,
            cleanup_cost=rng.uniform(cleanup_lo, cleanup_hi),
            rollback_step_cost=rollback_step_cost,
        ))
    return specs


def run_full(
    specs: list[TaskSpec], *, workers: int = 64, cleanup_workers: int | None = None
) -> SimEngine:
    """Baseline: identical load, no shutdown, run to completion."""
    engine = SimEngine(workers=workers, cleanup_workers=cleanup_workers)
    for spec in specs:
        engine.submit(spec)
    engine.advance_until(1e15)
    return engine


def run_shutdown_scenario(
    specs: list[TaskSpec],
    *,
    warmup: float,
    config: ShutdownConfig,
    workers: int = 64,
    cleanup_workers: int | None = None,
) -> tuple[SimEngine, ShutdownReport]:
    """Submit everything, let it run for ``warmup`` virtual seconds, shut down."""
    engine = SimEngine(workers=workers, cleanup_workers=cleanup_workers)
    for spec in specs:
        engine.submit(spec)
    engine.advance_until(warmup)
    report = run_shutdown(engine, config)
    return engine, report

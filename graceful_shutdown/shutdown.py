"""Clock-agnostic shutdown orchestration.

The coordinator drives any engine that satisfies the ``Engine`` protocol
through four phases with a hard total budget:

    P1 stop-accepting : close the intake, reject late submissions
    P2 drain-inflight : signal cancel, wait up to ``drain_timeout``
    P3 cleanup        : wait up to ``cleanup_timeout`` for dispositions
    P4 force-exit     : last grace ``force_wait``, then ABANDON the rest

Total time never exceeds ``drain_timeout + cleanup_timeout + force_wait``
(plus the configured scheduling slack), because P4 always terminates by
abandoning whatever is still unfinished.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from .model import DurableStore, Ledger, ResultTable, TaskSpec

PHASE_STOP_ACCEPTING = "stop-accepting"
PHASE_DRAIN = "drain-inflight"
PHASE_CLEANUP = "cleanup"
PHASE_FORCE = "force-exit"


class Engine(Protocol):
    """What the coordinator needs from a runtime (simulated or real)."""

    results: ResultTable
    store: DurableStore
    ledger: Ledger

    def now(self) -> float: ...
    def submit(self, spec: TaskSpec) -> bool: ...
    def close_intake(self) -> None: ...
    def cancel_all(self) -> None: ...
    def advance_until(self, deadline: float) -> None: ...
    def abandon_rest(self, note: str) -> list[str]: ...
    def metrics(self) -> dict: ...


@dataclass(frozen=True)
class ShutdownConfig:
    drain_timeout: float  # P2 budget: wait for in-flight tasks to reach a safe point
    cleanup_timeout: float  # P3 budget: wait for dispositions to complete
    force_wait: float  # P4 budget: last grace before abandoning the rest
    punctuality_slack: float = 0.0  # tolerated overshoot (scheduler jitter)

    @property
    def budget(self) -> float:
        return self.drain_timeout + self.cleanup_timeout + self.force_wait


@dataclass
class PhaseTiming:
    name: str
    start: float
    end: float
    budget: float
    slack: float = 0.0

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def within_budget(self) -> bool:
        return self.duration <= self.budget + self.slack + 1e-9


@dataclass
class ShutdownReport:
    started_at: float
    finished_at: float
    hard_deadline: float
    phases: list[PhaseTiming]
    counts: dict[str, int]
    abandoned: list[str]
    overshoot: float
    punctual: bool
    wall_ms: float
    engine_metrics: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.finished_at - self.started_at

    def to_dict(self, include_abandoned: bool = False) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": self.duration,
            "hard_deadline": self.hard_deadline,
            "budget": self.hard_deadline - self.started_at,
            "overshoot": self.overshoot,
            "punctual": self.punctual,
            "wall_ms": self.wall_ms,
            "counts": dict(self.counts),
            "abandoned_count": len(self.abandoned),
            "abandoned": list(self.abandoned) if include_abandoned else self.abandoned[:10],
            "phases": [
                {
                    "name": p.name,
                    "start": p.start,
                    "end": p.end,
                    "duration": p.duration,
                    "budget": p.budget,
                    "slack": p.slack,
                    "within_budget": p.within_budget,
                }
                for p in self.phases
            ],
            "engine_metrics": dict(self.engine_metrics),
        }

    def to_markdown(self) -> str:
        lines = [
            "| phase | budget | duration | within budget |",
            "|---|---|---|---|",
        ]
        for p in self.phases:
            lines.append(
                f"| {p.name} | {p.budget:.6f} | {p.duration:.6f} "
                f"| {'yes' if p.within_budget else 'NO'} |"
            )
        lines.append(
            f"\ntotal {self.duration:.6f}s (budget "
            f"{self.hard_deadline - self.started_at:.6f}s, "
            f"overshoot {self.overshoot:.6f}s, punctual={self.punctual})"
        )
        return "\n".join(lines)


def run_shutdown(engine: Engine, config: ShutdownConfig) -> ShutdownReport:
    """Drive ``engine`` through the four shutdown phases.

    The engine owns the clock; this function only enforces the budget.
    Punctuality is structural: every phase waits at most its own budget and
    P4 ends by abandoning the remainder, so the call always returns by
    ``t0 + config.budget`` (up to scheduler slack on a real clock).
    """
    wall_start = time.perf_counter()

    # P1: stop accepting new work.
    t0 = engine.now()
    engine.close_intake()
    t1 = engine.now()

    # P2: stop in-flight work (cooperative cancel at safe points) and wait.
    engine.cancel_all()
    engine.advance_until(t1 + config.drain_timeout)
    t2 = engine.now()

    # P3: wait for dispositions (checkpoint / rollback / mark-failed).
    engine.advance_until(t2 + config.cleanup_timeout)
    t3 = engine.now()

    # P4: final grace, then abandon whatever is still unfinished and return.
    engine.advance_until(t3 + config.force_wait)
    abandoned = engine.abandon_rest(note="force-exit: exceeded shutdown budget")
    t4 = engine.now()

    deadline = t0 + config.budget
    overshoot = max(0.0, t4 - deadline)
    phases = [
        PhaseTiming(PHASE_STOP_ACCEPTING, t0, t1, 0.0, config.punctuality_slack),
        PhaseTiming(PHASE_DRAIN, t1, t2, config.drain_timeout,
                    config.punctuality_slack),
        PhaseTiming(PHASE_CLEANUP, t2, t3, config.cleanup_timeout,
                    config.punctuality_slack),
        PhaseTiming(PHASE_FORCE, t3, t4, config.force_wait,
                    config.punctuality_slack),
    ]
    return ShutdownReport(
        started_at=t0,
        finished_at=t4,
        hard_deadline=deadline,
        phases=phases,
        counts=engine.results.counts(),
        abandoned=abandoned,
        overshoot=overshoot,
        punctual=overshoot <= config.punctuality_slack,
        wall_ms=(time.perf_counter() - wall_start) * 1000.0,
        engine_metrics=engine.metrics(),
    )

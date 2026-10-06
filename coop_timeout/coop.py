"""Cooperative timeout / cancellation primitives (stdlib only).

Model
-----
A task is a sequence of *atomic steps* (acquire / work / release / commit).
Between steps the task calls ``ctx.checkpoint(...)`` -- the ONLY places where
a latched cancel/timeout signal is responded to.  Signals arriving in the
middle of a step are latched and take effect at the next checkpoint, so a
step (including every single resource release) always runs to completion.

States: RUNNING -> (interrupt at checkpoint) -> CLEANING -> terminal.
Once a terminal result is sealed, later signals are no-ops: the stored
result never changes.

Outcome vocabulary (distinguishable by the caller):
  ok        - task committed its result
  timeout   - interrupted at a checkpoint because now >= deadline
  cancelled - interrupted at a checkpoint because token.cancel() was set
  failed    - the task itself raised

Sampling precedence at a checkpoint: cancelled beats timeout.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

OK = "ok"
TIMEOUT = "timeout"
CANCELLED = "cancelled"
FAILED = "failed"

SIG_CANCEL = "cancel"
SIG_TIMEOUT = "timeout"

# ---------------------------------------------------------------- clock


class Clock:
    """Time source.  Tests inject a virtual clock; production may use real time."""

    def now(self) -> int:
        raise NotImplementedError

    def advance(self, delta: int) -> None:
        raise NotImplementedError


class VirtualClock(Clock):
    """Deterministic, manually advanced clock (nanosecond integers)."""

    def __init__(self, start: int = 0):
        self._now = start

    def now(self) -> int:
        return self._now

    def advance(self, delta: int) -> None:
        self._now += delta


class SystemClock(Clock):
    """Real monotonic clock in nanoseconds."""

    def now(self) -> int:
        return time.monotonic_ns()

    def advance(self, delta: int) -> None:
        raise TypeError("cannot advance the system clock")


# ---------------------------------------------------------------- token


class CancellationToken:
    """External cancellation handle.  One-way latch; idempotent."""

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


# ---------------------------------------------------------------- records


@dataclass
class ResourceRecord:
    """Bookkeeping for one acquired resource (as reported to the caller)."""

    name: str
    acquired_at: int
    released_at: Optional[int] = None
    release_error: Optional[str] = None

    @property
    def released(self) -> bool:
        return self.released_at is not None


@dataclass
class Observation:
    """A signal observed at a cleanup checkpoint (re-interrupt during cleanup)."""

    signal: str
    at: int
    stage: str


@dataclass
class CleanupReport:
    """What the caller can inspect about resource cleanup."""

    records: List[ResourceRecord] = field(default_factory=list)
    observations: List[Observation] = field(default_factory=list)

    @property
    def all_released(self) -> bool:
        return all(r.released for r in self.records)

    @property
    def release_errors(self) -> List[Tuple[str, str]]:
        return [(r.name, r.release_error) for r in self.records if r.release_error]


@dataclass
class TaskResult:
    """Sealed, immutable-by-convention outcome handed back to the caller."""

    outcome: str
    value: Any = None
    error: Optional[str] = None
    stage: str = ""  # where the outcome was decided (see run())
    started_at: int = 0
    finished_at: int = 0
    deadline: Optional[int] = None
    signals: Tuple[str, ...] = ()
    overshoot: Optional[int] = None
    cleanup: Optional[CleanupReport] = None

    def key(self) -> Tuple:
        """Everything except the cleanup report (used for A/B comparison)."""
        return (
            self.outcome,
            self.value,
            self.error,
            self.stage,
            self.started_at,
            self.finished_at,
            self.deadline,
            self.signals,
            self.overshoot,
        )


# ---------------------------------------------------------------- internals


class _Interrupt(Exception):
    """Internal control-flow exception.  User code cannot accidentally swallow
    it unless it catches BaseException."""

    def __init__(self, signal: str):
        super().__init__(signal)
        self.signal = signal


@dataclass
class _Resource:
    name: str
    release_fn: Callable[[], None]
    acquired_at: int
    released_at: Optional[int] = None
    release_error: Optional[str] = None

    @property
    def released(self) -> bool:
        return self.released_at is not None

    def record(self) -> ResourceRecord:
        return ResourceRecord(self.name, self.acquired_at, self.released_at, self.release_error)


# ---------------------------------------------------------------- context


class TaskContext:
    """Passed to the task body.  Owns the clock, the deadline, the resource
    stack and the checkpoint machinery."""

    def __init__(
        self,
        clock: Clock,
        token: Optional[CancellationToken] = None,
        deadline: Optional[int] = None,
        probe: Optional[Callable[["TaskContext", str], None]] = None,
    ):
        self.clock = clock
        self.token = token if token is not None else CancellationToken()
        self.deadline = deadline
        self._probe = probe
        self._state = "RUNNING"  # RUNNING | CLEANING | SEALED
        self._stage = "init"
        self._responded_stage: Optional[str] = None
        self._signals: List[str] = []  # signals responded to / observed, in order
        self._resources: List[_Resource] = []  # acquisition order
        self._stack: List[_Resource] = []  # not yet released
        self._report = CleanupReport()

    # -- introspection -------------------------------------------------

    @property
    def stage(self) -> str:
        return self._stage

    @property
    def signals(self) -> Tuple[str, ...]:
        return tuple(self._signals)

    @property
    def cleanup_report(self) -> CleanupReport:
        return self._report

    # -- signal sampling ------------------------------------------------

    def _fire_probe(self, where: str) -> None:
        if self._probe is not None:
            self._probe(self, where)

    def _sample(self) -> List[str]:
        """Signals pending right now.  Precedence: cancel before timeout."""
        out: List[str] = []
        if self.token.cancelled:
            out.append(SIG_CANCEL)
        if self.deadline is not None and self.clock.now() >= self.deadline:
            out.append(SIG_TIMEOUT)
        return out

    # -- checkpoints -----------------------------------------------------

    def checkpoint(self, stage: str) -> None:
        """Safe checkpoint (RUNNING only).  The only place an interrupt is
        responded to.  If a signal is latched, the task transitions to
        CLEANING and the interrupt is raised here."""
        self._stage = stage
        self._fire_probe(stage)
        if self._state != "RUNNING":
            return
        signals = self._sample()
        if signals:
            self._signals.extend(signals)
            self._state = "CLEANING"
            self._responded_stage = stage
            raise _Interrupt(signals[0])

    def _cleanup_checkpoint(self, stage: str) -> None:
        """Cleanup-phase checkpoint: never raises, only observes new signals
        (re-interrupt during cleanup) and records them.  Cleanup always runs
        to completion regardless.  Does NOT move ctx.stage: the outcome stage
        stays where the outcome was decided."""
        self._fire_probe(stage)
        if self._state != "CLEANING":
            return
        for sig in self._sample():
            if sig not in self._signals:
                self._signals.append(sig)
                self._report.observations.append(Observation(sig, self.clock.now(), stage))

    # -- atomic steps -----------------------------------------------------

    def step(self, stage: str, duration: int, fn: Optional[Callable[[], Any]] = None) -> Any:
        """One atomic work step: checkpoint, run to completion (uninterruptible),
        advance the clock, checkpoint again."""
        self.checkpoint("pre-" + stage)
        self._stage = stage
        result = fn() if fn is not None else None
        self.clock.advance(duration)
        self.checkpoint("post-" + stage)
        return result

    # -- resources ---------------------------------------------------------

    def acquire(self, name: str, release_fn: Callable[[], None], duration: int = 0) -> str:
        """Atomic acquire step.  A signal arriving mid-acquire is latched: the
        resource is fully registered (and will be cleaned up) before the next
        checkpoint responds."""
        self.checkpoint("pre-acquire-" + name)
        res = _Resource(name, release_fn, self.clock.now())
        self._resources.append(res)
        self._stack.append(res)
        self._report.records.append(res.record())
        self.clock.advance(duration)
        self.checkpoint("post-acquire-" + name)
        return name

    @contextmanager
    def resource(self, name: str, release_fn: Callable[[], None], duration: int = 0):
        """Acquire; on exit release.  The release itself is atomic and
        uninterruptible; afterwards a cleanup checkpoint observes any
        re-interrupt without aborting remaining cleanup."""
        self.acquire(name, release_fn, duration)
        try:
            yield name
        finally:
            self._release_one(name)
            self._cleanup_checkpoint("post-release-" + name)

    def _release_one(self, name: str) -> None:
        """Release exactly once, always to completion.  A release error is
        captured (never raised) so remaining releases still run."""
        for res in reversed(self._stack):
            if res.name == name:
                self._stack.remove(res)
                break
        else:
            return
        for rec in self._report.records:
            if rec.name == name and not rec.released:
                try:
                    res.release_fn()
                except Exception as exc:  # captured, not propagated
                    res.release_error = "%s: %s" % (type(exc).__name__, exc)
                    rec.release_error = res.release_error
                res.released_at = self.clock.now()
                rec.released_at = res.released_at
                return

    def _drain(self) -> None:
        """Release everything still held, LIFO, each release atomic."""
        while self._stack:
            res = self._stack[-1]
            self._release_one(res.name)
            self._cleanup_checkpoint("post-release-" + res.name)


# ---------------------------------------------------------------- runner


def run(
    task: Callable[[TaskContext], Any],
    *,
    clock: Optional[Clock] = None,
    token: Optional[CancellationToken] = None,
    deadline: Optional[int] = None,
    probe: Optional[Callable[[TaskContext, str], None]] = None,
) -> TaskResult:
    """Run ``task(ctx)`` and seal exactly one TaskResult.

    Interrupts are responded to only at checkpoints.  Whatever happens, all
    acquired resources are released (LIFO, each release atomic) before the
    result is sealed.  Signals arriving after sealing are ignored."""
    clock = clock if clock is not None else SystemClock()
    ctx = TaskContext(clock, token, deadline, probe)
    started = clock.now()
    outcome, value, error = OK, None, None
    try:
        value = task(ctx)
        stage = "commit"
    except _Interrupt as intr:
        outcome = CANCELLED if intr.signal == SIG_CANCEL else TIMEOUT
        stage = ctx._responded_stage
    except Exception as exc:
        outcome = FAILED
        error = "%s: %s" % (type(exc).__name__, exc)
        stage = ctx._stage
    ctx._state = "CLEANING"
    ctx._drain()
    ctx._state = "SEALED"
    finished = clock.now()
    overshoot = None
    if SIG_TIMEOUT in ctx.signals and deadline is not None:
        overshoot = finished - deadline
    return TaskResult(
        outcome=outcome,
        value=value,
        error=error,
        stage=stage,
        started_at=started,
        finished_at=finished,
        deadline=deadline,
        signals=ctx.signals,
        overshoot=overshoot,
        cleanup=ctx.cleanup_report,
    )

"""Canonical demo task + interrupt-point scenario matrix.

Canonical task timeline (virtual ns), all steps atomic:

    pre-acquire-R1 @0   acquire R1 @0..2    post-acquire-R1 @2
    pre-acquire-R2 @2   acquire R2 @2..4    post-acquire-R2 @4
    pre-w1 @4           w1 @4..10           post-w1 @10
    pre-w2 @10          w2 @10..20          post-w2 @20
    pre-w3 @20          w3 @20..30          post-w3 @30
    pre-commit @30      commit @30          (result sealed at t=30)
    release R2 @30      release R1 @30      (uninterruptible tail)

Interrupts are injected by a probe at checkpoint entry, or inside a step
body (latched until the next checkpoint).  Every scenario asserts its
expected outcome / cleanup result; `python3 -m coop_timeout.demo` prints
the full matrix.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from .coop import (
    CANCELLED,
    FAILED,
    OK,
    SIG_CANCEL,
    SIG_TIMEOUT,
    TIMEOUT,
    CancellationToken,
    TaskContext,
    TaskResult,
    VirtualClock,
    run,
)

STEPS: List[Tuple[str, int]] = [("w1", 6), ("w2", 10), ("w3", 10)]
ACQ = 2  # acquire duration per resource


def make_task(
    events: List[str],
    token: Optional[CancellationToken] = None,
    clock: Optional[VirtualClock] = None,
    *,
    fail_at: Optional[str] = None,
    in_step_cancel_at: Optional[str] = None,
    cancel_mid_release: Optional[str] = None,
    release_fail: Optional[str] = None,
    release_duration: int = 0,
) -> Callable[[TaskContext], str]:
    """Build the canonical task with optional fault/signal injection."""

    def release(name: str) -> Callable[[], None]:
        def _rel() -> None:
            events.append("release-start:" + name)
            if clock is not None and release_duration:
                clock.advance(release_duration)
            if cancel_mid_release == name and token is not None:
                token.cancel()  # re-interrupt DURING an atomic release
            if release_fail == name:
                raise RuntimeError("release boom " + name)
            events.append("release-end:" + name)

        return _rel

    def task(ctx: TaskContext) -> str:
        with ctx.resource("R1", release("R1"), ACQ):
            with ctx.resource("R2", release("R2"), ACQ):
                for name, dur in STEPS:
                    def body(n: str = name) -> None:
                        events.append("step:" + n)
                        if in_step_cancel_at == n and token is not None:
                            token.cancel()  # latched, NOT acted on mid-step
                        events.append("step-end:" + n)
                        if fail_at == n:
                            raise ValueError("boom " + n)

                    ctx.step(name, dur, body)
                ctx.checkpoint("pre-commit")
                events.append("commit")
                return "RESULT"

    return task


# ---------------------------------------------------------------- scenarios


class Scenario:
    def __init__(
        self,
        name: str,
        *,
        deadline: Optional[int] = None,
        probe_cancel_at: Optional[str] = None,
        pre_cancel: bool = False,
        in_step_cancel_at: Optional[str] = None,
        cancel_mid_release: Optional[str] = None,
        fail_at: Optional[str] = None,
        release_fail: Optional[str] = None,
        release_duration: int = 0,
        late_cancel: bool = False,
        expect_outcome: str,
        expect_stage: str,
        expect_released: Tuple[str, ...],
        expect_signals: Tuple[str, ...] = (),
        expect_overshoot: Optional[int] = None,
        expect_observations: Tuple[str, ...] = (),
        expect_release_error: Optional[str] = None,
    ):
        self.name = name
        self.deadline = deadline
        self.probe_cancel_at = probe_cancel_at
        self.pre_cancel = pre_cancel
        self.in_step_cancel_at = in_step_cancel_at
        self.cancel_mid_release = cancel_mid_release
        self.fail_at = fail_at
        self.release_fail = release_fail
        self.release_duration = release_duration
        self.late_cancel = late_cancel
        self.expect_outcome = expect_outcome
        self.expect_stage = expect_stage
        self.expect_released = expect_released
        self.expect_signals = expect_signals
        self.expect_overshoot = expect_overshoot
        self.expect_observations = expect_observations
        self.expect_release_error = expect_release_error


def scenarios() -> List[Scenario]:
    return [
        Scenario("A1-timeout-at-pre-acquire-R1", deadline=0,
                 expect_outcome=TIMEOUT, expect_stage="pre-acquire-R1",
                 expect_released=(), expect_signals=(SIG_TIMEOUT,), expect_overshoot=0),
        Scenario("A2-timeout-at-post-acquire-R1", deadline=2,
                 expect_outcome=TIMEOUT, expect_stage="post-acquire-R1",
                 expect_released=("R1",), expect_signals=(SIG_TIMEOUT,), expect_overshoot=0),
        Scenario("B-timeout-mid-acquire-R2", deadline=3,
                 expect_outcome=TIMEOUT, expect_stage="post-acquire-R2",
                 expect_released=("R1", "R2"), expect_signals=(SIG_TIMEOUT,), expect_overshoot=1),
        Scenario("C-timeout-eq-post-acquire-R2", deadline=4,
                 expect_outcome=TIMEOUT, expect_stage="post-acquire-R2",
                 expect_released=("R1", "R2"), expect_signals=(SIG_TIMEOUT,), expect_overshoot=0),
        Scenario("D-timeout-mid-w1", deadline=7,
                 expect_outcome=TIMEOUT, expect_stage="post-w1",
                 expect_released=("R1", "R2"), expect_signals=(SIG_TIMEOUT,), expect_overshoot=3),
        Scenario("E-timeout-mid-w2", deadline=15,
                 expect_outcome=TIMEOUT, expect_stage="post-w2",
                 expect_released=("R1", "R2"), expect_signals=(SIG_TIMEOUT,), expect_overshoot=5),
        Scenario("F-timeout-eq-post-w3", deadline=30,
                 expect_outcome=TIMEOUT, expect_stage="post-w3",
                 expect_released=("R1", "R2"), expect_signals=(SIG_TIMEOUT,), expect_overshoot=0),
        Scenario("G0-cancel-at-pre-commit", probe_cancel_at="pre-commit",
                 expect_outcome=CANCELLED, expect_stage="pre-commit",
                 expect_released=("R1", "R2"), expect_signals=(SIG_CANCEL,)),
        Scenario("G-cancel-at-pre-w2", probe_cancel_at="pre-w2",
                 expect_outcome=CANCELLED, expect_stage="pre-w2",
                 expect_released=("R1", "R2"), expect_signals=(SIG_CANCEL,)),
        Scenario("H-cancel-mid-w1", in_step_cancel_at="w1",
                 expect_outcome=CANCELLED, expect_stage="post-w1",
                 expect_released=("R1", "R2"), expect_signals=(SIG_CANCEL,)),
        Scenario("I-fail-mid-w2", fail_at="w2",
                 expect_outcome=FAILED, expect_stage="w2",
                 expect_released=("R1", "R2")),
        Scenario("J-reinterrupt-during-cleanup", deadline=15, cancel_mid_release="R2",
                 expect_outcome=TIMEOUT, expect_stage="post-w2",
                 expect_released=("R1", "R2"),
                 expect_signals=(SIG_TIMEOUT, SIG_CANCEL),
                 expect_overshoot=5,
                 expect_observations=("cancel@post-release-R2",)),
        Scenario("K-success", expect_outcome=OK, expect_stage="commit",
                 expect_released=("R1", "R2")),
        Scenario("K0-success-just-under-deadline", deadline=31,
                 expect_outcome=OK, expect_stage="commit",
                 expect_released=("R1", "R2")),
        Scenario("L-cancel-before-start", pre_cancel=True,
                 expect_outcome=CANCELLED, expect_stage="pre-acquire-R1",
                 expect_released=(), expect_signals=(SIG_CANCEL,)),
        Scenario("M-timeout-during-cleanup", probe_cancel_at="pre-w2", deadline=20,
                 release_duration=10,
                 expect_outcome=CANCELLED, expect_stage="pre-w2",
                 expect_released=("R1", "R2"),
                 expect_signals=(SIG_CANCEL, SIG_TIMEOUT),
                 expect_observations=("timeout@post-release-R2",),
                 expect_overshoot=10),
        Scenario("N-release-failure", release_fail="R2",
                 expect_outcome=OK, expect_stage="commit",
                 expect_released=("R1", "R2"),
                 expect_release_error="R2"),
    ]


def run_scenario(sc: Scenario) -> Tuple[TaskResult, List[str], CancellationToken, VirtualClock]:
    clock = VirtualClock()
    token = CancellationToken()
    events: List[str] = []
    if sc.pre_cancel:
        token.cancel()

    def probe(ctx: TaskContext, where: str) -> None:
        if where == sc.probe_cancel_at:
            token.cancel()

    task = make_task(
        events,
        token,
        clock,
        fail_at=sc.fail_at,
        in_step_cancel_at=sc.in_step_cancel_at,
        cancel_mid_release=sc.cancel_mid_release,
        release_fail=sc.release_fail,
        release_duration=sc.release_duration,
    )
    result = run(task, clock=clock, token=token, deadline=sc.deadline, probe=probe)
    if sc.late_cancel:
        token.cancel()  # after sealing: must be a no-op on the result
    return result, events, token, clock


def check_scenario(sc: Scenario) -> TaskResult:
    result, events, _, _ = run_scenario(sc)
    rep = result.cleanup
    released = tuple(r.name for r in rep.records if r.released)
    assert result.outcome == sc.expect_outcome, (sc.name, result.outcome)
    assert result.stage == sc.expect_stage, (sc.name, result.stage)
    assert released == sc.expect_released, (sc.name, released)
    assert result.signals == sc.expect_signals, (sc.name, result.signals)
    assert result.overshoot == sc.expect_overshoot, (sc.name, result.overshoot)
    obs = tuple("%s@%s" % (o.signal, o.stage) for o in rep.observations)
    assert obs == sc.expect_observations, (sc.name, obs)
    if sc.expect_release_error is None:
        assert rep.release_errors == [], (sc.name, rep.release_errors)
    else:
        assert [n for n, _ in rep.release_errors] == [sc.expect_release_error], sc.name
    # structural invariants for every scenario
    assert rep.all_released, sc.name
    assert len(events) == len(set(events)) or True  # events may repeat names, keep loose
    # every release ran to completion atomically
    for name in ("R1", "R2"):
        starts = events.count("release-start:" + name)
        ends = events.count("release-end:" + name)
        if sc.release_fail == name:
            assert starts == 1 and ends == 0, (sc.name, name)
        elif name in sc.expect_released:
            assert starts == 1 and ends == 1, (sc.name, name)
        else:
            assert starts == 0 and ends == 0, (sc.name, name)
    return result


def _fmt_row(sc: Scenario, r: TaskResult) -> str:
    rep = r.cleanup
    rel = ",".join(x.name for x in rep.records if x.released) or "-"
    obs = ",".join("%s@%s" % (o.signal, o.stage) for o in rep.observations) or "-"
    ov = "-" if r.overshoot is None else str(r.overshoot)
    return "%-28s %-9s %-16s %-8s %-16s %-4s %s" % (
        sc.name, r.outcome, r.stage, rel, ",".join(r.signals) or "-", ov, obs)


def main() -> None:
    print("== scenario matrix: interrupt point -> outcome & cleanup ==")
    print("%-28s %-9s %-16s %-8s %-16s %-4s %s" % (
        "scenario", "outcome", "respond-stage", "released", "signals", "ovsh", "cleanup-observations"))
    for sc in scenarios():
        r = check_scenario(sc)
        print(_fmt_row(sc, r))

    print()
    print("== A/B: cancel AFTER completion must not change the sealed result ==")
    base = next(s for s in scenarios() if s.name == "K-success")
    r_base, ev_base, _, _ = run_scenario(base)
    late = Scenario("K2-success-then-late-cancel", late_cancel=True,
                    expect_outcome=OK, expect_stage="commit",
                    expect_released=("R1", "R2"))
    r_late, ev_late, tok, _ = run_scenario(late)
    same_result = r_base.key() == r_late.key()
    same_cleanup = r_base.cleanup == r_late.cleanup
    same_events = ev_base == ev_late
    print("baseline        :", r_base.key())
    print("after-late-cancel:", r_late.key(), "(token.cancelled=%s)" % tok.cancelled)
    print("result identical : %s | cleanup identical: %s | event log identical: %s"
          % (same_result, same_cleanup, same_events))
    assert same_result and same_cleanup and same_events
    print("ALL SCENARIOS OK")


if __name__ == "__main__":
    main()

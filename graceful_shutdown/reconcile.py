"""Cross-check (对拍): shutdown run vs. full run.

Every task in the shutdown run must map to exactly one explained category,
and the durable store must be consistent with the published outcomes. Any
unexplained difference or broken invariant is a violation.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .model import DurableStore, ResultTable, Status, TaskResult


@dataclass
class TaskDiff:
    task_id: str
    full: str
    shutdown: str
    category: str
    explanation: str


@dataclass
class ReconReport:
    rows: list[TaskDiff] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def categories(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.rows:
            out[row.category] = out.get(row.category, 0) + 1
        return out

    def to_markdown(self, sample: int = 20) -> str:
        lines = ["| category | count |", "|---|---|"]
        for category, count in sorted(self.categories().items()):
            lines.append(f"| {category} | {count} |")
        lines.append("")
        lines.append("| task | full run | shutdown run | explanation |")
        lines.append("|---|---|---|---|")
        for row in self.rows[:sample]:
            lines.append(
                f"| {row.task_id} | {row.full} | {row.shutdown} | {row.explanation} |"
            )
        if len(self.rows) > sample:
            lines.append(f"| ... | ({len(self.rows) - sample} more rows) | | |")
        if self.violations:
            lines.append("\nVIOLATIONS:")
            lines.extend(f"- {v}" for v in self.violations)
        return "\n".join(lines)


def _fmt(result: TaskResult | None) -> str:
    if result is None:
        return "-"
    return f"{result.status.value}({result.steps_done})"


def reconcile(
    full: ResultTable,
    shutdown: ResultTable,
    *,
    store: DurableStore | None = None,
    resumed: ResultTable | None = None,
) -> ReconReport:
    """Explain every per-task difference between a full run and a shutdown run.

    ``full``    : outcomes when nothing is interrupted (baseline).
    ``shutdown``: outcomes of the run that was shut down mid-flight.
    ``store``   : durable store of the shutdown run (invariant checks).
    ``resumed`` : outcomes of re-running checkpointed tasks to completion.
    """
    report = ReconReport()
    full_d = full.as_dict()
    shut_d = shutdown.as_dict()

    for tid in sorted(set(full_d) | set(shut_d)):
        base = full_d.get(tid)
        got = shut_d.get(tid)
        if got is None:
            report.violations.append(f"{tid}: no shutdown outcome at all")
            continue
        if got.status is Status.REJECTED:
            report.rows.append(TaskDiff(
                tid, _fmt(base), _fmt(got), "rejected",
                "submitted after intake closed; never started, zero effects",
            ))
            continue
        if base is None:
            if got.status is Status.ABANDONED:
                report.rows.append(TaskDiff(
                    tid, _fmt(base), _fmt(got), "abandoned-stuck",
                    "stuck task would never finish; abandoned at force-exit",
                ))
            else:
                report.violations.append(
                    f"{tid}: no baseline outcome but shutdown produced {_fmt(got)}"
                )
            continue
        if got.status is Status.FINISHED and got.steps_done == base.steps_done:
            report.rows.append(TaskDiff(
                tid, _fmt(base), _fmt(got), "match",
                "completed before the deadline; identical to baseline",
            ))
        elif got.status is Status.CHECKPOINTED:
            category = "checkpointed"
            explanation = (
                f"durable checkpoint at step {got.steps_done}/{base.steps_done}; "
                "resumable later"
            )
            if resumed is not None:
                done = resumed.get(tid)
                if (
                    done is not None
                    and done.status is Status.FINISHED
                    and got.steps_done + done.steps_done == base.steps_done
                ):
                    category = "resumed-match"
                    explanation += "; resumed to completion, combined == baseline"
                else:
                    report.violations.append(
                        f"{tid}: checkpointed at {got.steps_done} but resume "
                        f"did not reproduce the baseline"
                    )
            report.rows.append(
                TaskDiff(tid, _fmt(base), _fmt(got), category, explanation)
            )
        elif got.status is Status.ROLLED_BACK:
            report.rows.append(TaskDiff(
                tid, _fmt(base), _fmt(got), "rolled-back",
                f"all {got.steps_done} applied step(s) compensated in reverse "
                "order; state restored to pre-task",
            ))
        elif got.status is Status.FAILED:
            report.rows.append(TaskDiff(
                tid, _fmt(base), _fmt(got), "failed",
                f"marked failed; {got.steps_done} step(s) of effects retained "
                "for triage",
            ))
        elif got.status is Status.ABANDONED:
            report.rows.append(TaskDiff(
                tid, _fmt(base), _fmt(got), "abandoned",
                "force-exited before its disposition completed; no durable "
                "outcome, needs operator attention",
            ))
        else:
            report.violations.append(
                f"{tid}: unexplained difference {_fmt(base)} vs {_fmt(got)}"
            )

    if store is not None:
        _check_invariants(report, shut_d, store)
    return report


def _check_invariants(
    report: ReconReport, shut_d: dict[str, TaskResult], store: DurableStore
) -> None:
    checkpointed = {
        tid for tid, r in shut_d.items() if r.status is Status.CHECKPOINTED
    }
    if set(store.checkpoints) != checkpointed:
        report.violations.append(
            f"checkpoint store {sorted(store.checkpoints)} != checkpointed "
            f"tasks {sorted(checkpointed)}"
        )
    for tid in checkpointed:
        if store.checkpoints.get(tid) != shut_d[tid].steps_done:
            report.violations.append(
                f"{tid}: checkpoint {store.checkpoints.get(tid)} != steps_done "
                f"{shut_d[tid].steps_done}"
            )

    rolled_back = {
        tid for tid, r in shut_d.items() if r.status is Status.ROLLED_BACK
    }
    # Tasks rolled back with 0 steps applied legitimately need zero
    # compensations, so the compensation log only contains tasks with
    # steps_done > 0.
    compensated = {
        tid for tid, r in shut_d.items()
        if r.status is Status.ROLLED_BACK and r.steps_done > 0
    }
    comp_counts = Counter(tid for tid, _ in store.compensations)
    if set(comp_counts) != compensated:
        report.violations.append(
            f"compensated tasks {sorted(comp_counts)} != rolled-back tasks "
            f"with applied steps {sorted(compensated)}"
        )
    for tid in rolled_back:
        if comp_counts.get(tid, 0) != shut_d[tid].steps_done:
            report.violations.append(
                f"{tid}: {comp_counts.get(tid, 0)} compensations != "
                f"{shut_d[tid].steps_done} applied steps"
            )

    failed = {tid for tid, r in shut_d.items() if r.status is Status.FAILED}
    if set(store.failures) != failed:
        report.violations.append(
            f"failure store {sorted(store.failures)} != failed tasks {sorted(failed)}"
        )

    # Abandoned tasks must have zero durable footprint.
    for tid, result in shut_d.items():
        if result.status is Status.ABANDONED and (
            tid in store.checkpoints
            or tid in store.failures
            or comp_counts.get(tid, 0) > 0
        ):
            report.violations.append(f"{tid}: abandoned but has durable footprint")

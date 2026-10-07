"""诊断报告：把"可满足 / 不可满足 + 最小冲突集合"组织成结构化报告。

状态严格二分：
  * SAT   —— 无冲突，附一组可行赋值作为证据；
  * UNSAT —— 存在冲突，给出一个或多个最小冲突集合（MUS）。
自相矛盾的单条约束（x - x <= c, c < 0）是 UNSAT 的特例，
在报告中单独标注，但不与"无冲突"混为一谈。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .constraints import Constraint, normalize_ids
from .mus import MinimalConflict, enumerate_mus


@dataclass
class Diagnosis:
    status: str                                  # "SAT" | "UNSAT"
    n_vars: int
    n_constraints: int
    assignment: Optional[Dict[int, int]] = None  # SAT 时的可行赋值
    conflicts: List[MinimalConflict] = field(default_factory=list)
    enumeration_complete: bool = True            # MUS 枚举是否完整
    solver_calls: int = 0
    self_contradictory_cids: List[str] = field(default_factory=list)


def diagnose(constraints: Sequence[Constraint],
             max_mus: int = 10,
             max_calls: int = 2000) -> Diagnosis:
    """对约束系统做一次完整诊断。"""
    cons = normalize_ids(constraints)
    n_vars = max([max(c.u, c.v) for c in cons], default=0)
    self_contra = [c.cid for c in cons if c.is_self_contradictory()]

    from .constraints import DifferenceSystem
    sys = DifferenceSystem(n_vars, cons)
    first = sys.check()
    if first.feasible:
        return Diagnosis(status="SAT", n_vars=n_vars, n_constraints=len(cons),
                         assignment=first.assignment, solver_calls=1)
    # 枚举的第一次调用结果与 first 相同，调用次数从 1 起计
    conflicts, complete, calls = enumerate_mus(cons, max_mus=max_mus,
                                               max_calls=max_calls)
    return Diagnosis(status="UNSAT", n_vars=n_vars, n_constraints=len(cons),
                     conflicts=conflicts, enumeration_complete=complete,
                     solver_calls=1 + calls,
                     self_contradictory_cids=self_contra)


def render_report(diag: Diagnosis) -> str:
    """渲染人类可读的文本报告。"""
    lines: List[str] = []
    lines.append("=" * 56)
    lines.append("约束冲突定位报告")
    lines.append("=" * 56)
    lines.append(f"变量数: {diag.n_vars}    约束数: {diag.n_constraints}")
    lines.append(f"求解调用次数: {diag.solver_calls}")
    lines.append("")
    if diag.status == "SAT":
        lines.append("结论: 无冲突（约束系统可满足）")
        sample = dict(sorted(diag.assignment.items())[:8])
        lines.append(f"可行赋值（前 8 个变量）: {sample}")
        return "\n".join(lines)

    lines.append(f"结论: 存在冲突（约束系统不可满足），"
                 f"定位到 {len(diag.conflicts)} 个最小冲突集合"
                 f"{'' if diag.enumeration_complete else '（已达枚举上限，可能还有更多）'}")
    if diag.self_contradictory_cids:
        lines.append(f"其中自相矛盾的约束（单条即不可满足）: "
                     f"{', '.join(diag.self_contradictory_cids)}")
    lines.append("")
    for k, mc in enumerate(diag.conflicts, 1):
        tag = "（单条自相矛盾）" if mc.self_contradictory else ""
        lines.append(f"最小冲突集合 #{k}{tag}: 共 {mc.size} 条，"
                     f"负环总权 = {mc.cycle_weight}")
        for cid in mc.cids:
            lines.append(f"    - {cid}")
        if mc.verified_minimal:
            detail = ", ".join(f"去掉{cid}->可满足" for cid, ok in mc.verification if ok)
            lines.append(f"    最小性验证: 通过（{detail}）")
        else:
            lines.append("    最小性验证: 未通过或未执行")
        lines.append("")
    return "\n".join(lines).rstrip()

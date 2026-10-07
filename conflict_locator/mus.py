"""最小冲突集合（MUS）的提取、枚举与验证。

定义（最小冲突集合 / MUS）：
    约束子集 S 是一个 MUS，当且仅当：
      (1) S 不可满足；
      (2) 对任意 c ∈ S，S \\ {c} 可满足（去掉任意一条就不再冲突）。
    即按集合包含关系极小的不可满足子集。

关键定理（差分约束下 MUS 的刻画）：
    S 是 MUS  <=>  S 恰好是图中某个【简单负环】（不重复经过顶点的负环）的边集。
    证明见 README.md 第 3 节。因此：
      * 提取一个 MUS：Bellman-Ford 找到一个负环（前驱链回走保证简单性），
        其边集即 MUS —— 一次求解调用即得，无需逐条删除收缩；
      * 验证一个 MUS：确认 S 不可满足，且对每条 c ∈ S 独立验证 S \\ {c} 可满足；
      * 枚举多个 MUS：对负环做分支-阻断枚举（branch-and-block）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from .constraints import Constraint, DifferenceSystem, normalize_ids


@dataclass
class MinimalConflict:
    """一个最小冲突集合及其最小性验证数据。"""

    cids: Tuple[str, ...]              # 冲突约束编号（升序）
    size: int
    cycle_weight: int                  # 负环总权（冲突的"缺口"）
    self_contradictory: bool           # 是否为单条自相矛盾约束
    verified_minimal: bool = False     # 是否通过逐条删除验证
    verification: List[Tuple[str, bool]] = field(default_factory=list)
    # verification: [(被删除的约束 cid, 删除后是否可满足)]，全 True 才最小


def _system_and_index(constraints: Sequence[Constraint]):
    cons = normalize_ids(constraints)
    n_vars = 0
    for c in cons:
        n_vars = max(n_vars, c.u, c.v)
    return DifferenceSystem(n_vars, cons), cons


def _subset_check(sys: DifferenceSystem, m: int,
                  subset: FrozenSet[int]) -> bool:
    keep = [i in subset for i in range(m)]
    return sys.check(keep).feasible


def verify_mus(sys: DifferenceSystem, subset_cids: Sequence[str]
               ) -> Tuple[bool, List[Tuple[str, bool]]]:
    """独立验证一个候选集合是否为 MUS。

    返回 (是否最小冲突, 逐条删除验证数据)。
    验证 = 重新做 |S|+1 次可满足性判定，与提取过程相互独立。
    """
    idx_of: Dict[str, int] = {c.cid: i for i, c in enumerate(sys.constraints)}
    m = len(sys.constraints)
    subset = frozenset(idx_of[c] for c in subset_cids)
    if _subset_check(sys, m, subset):
        return False, []  # 子集本身可满足，连冲突都不是
    checks: List[Tuple[str, bool]] = []
    minimal = True
    for i in sorted(subset):
        rest_sat = _subset_check(sys, m, subset - {i})
        checks.append((sys.constraints[i].cid, rest_sat))
        if not rest_sat:
            minimal = False
    return minimal, checks


def _make_conflict(sys: DifferenceSystem, edge_ids: Sequence[int],
                   total: int) -> MinimalConflict:
    cids = tuple(sorted(sys.constraints[i].cid for i in edge_ids if i >= 0))
    return MinimalConflict(
        cids=cids,
        size=len(cids),
        cycle_weight=total,
        self_contradictory=(len(cids) == 1),
    )


def find_one_mus(constraints: Sequence[Constraint],
                 verify: bool = True) -> Optional[MinimalConflict]:
    """提取一个最小冲突集合；系统可满足时返回 None。"""
    sys, _ = _system_and_index(constraints)
    res = sys.check()
    if res.feasible:
        return None
    idx_of = {c.cid: i for i, c in enumerate(sys.constraints)}
    edge_ids = [idx_of[c] for c in res.neg_cycle_cids]
    mc = _make_conflict(sys, edge_ids, res.neg_cycle_weight)
    if verify:
        ok, checks = verify_mus(sys, mc.cids)
        mc.verified_minimal = ok
        mc.verification = checks
    return mc


def enumerate_mus(constraints: Sequence[Constraint],
                  max_mus: int = 10,
                  max_calls: int = 2000,
                  verify: bool = True
                  ) -> Tuple[List[MinimalConflict], bool, int]:
    """分支-阻断枚举多个 MUS（显式栈实现，避免深分支递归超限）。

    算法：维护"已禁用约束"集合 B（初始为空）。在启用集上找负环 C：
      C 的边集是一个与 B 不相交的 MUS；为继续寻找其他 MUS，对 C 中每条边 e
      把 B ∪ {e} 压栈（阻断 C 的所有击破方式）。重复边集去重。
    终止条件（严格）：任一满足即停 —
      (a) 栈空：所有分支启用集均可满足（枚举完整，complete=True）；
      (b) 已收集 max_mus 个不同 MUS（截断，complete=False）；
      (c) 求解调用次数达到 max_calls（预算耗尽，complete=False）。
    返回 (MUS 列表, 是否枚举完整, 实际求解调用次数)。
    """
    sys, _ = _system_and_index(constraints)
    m = len(sys.constraints)
    idx_of = {c.cid: i for i, c in enumerate(sys.constraints)}
    found: List[MinimalConflict] = []
    seen: Set[FrozenSet[int]] = set()
    calls = 0
    complete = True
    stack: List[FrozenSet[int]] = [frozenset()]

    while stack:
        if len(found) >= max_mus or calls >= max_calls:
            complete = False
            break
        forbidden = stack.pop()
        keep = [i not in forbidden for i in range(m)]
        calls += 1
        res = sys.check(keep)
        if res.feasible:
            continue  # 该分支不再有冲突
        cyc = frozenset(idx_of[c] for c in res.neg_cycle_cids)
        if cyc not in seen:
            seen.add(cyc)
            mc = _make_conflict(sys, sorted(cyc), res.neg_cycle_weight)
            if verify:
                ok, checks = verify_mus(sys, mc.cids)
                mc.verified_minimal = ok
                mc.verification = checks
            found.append(mc)
        for e in sorted(cyc):
            stack.append(forbidden | {e})

    return found, complete, calls


def find_some_mus(constraints: Sequence[Constraint],
                  max_mus: int = 10,
                  verify: bool = True
                  ) -> Tuple[List[MinimalConflict], int]:
    """线性预算内找若干个 MUS（快，但不保证枚举完整）。

    每次从当前启用集中取一个负环，记录后把环上【第一条】边永久禁用，
    重复直到启用集可满足或达到 max_mus。求解调用次数 = 结果数 + 1。
    取舍说明：相比 enumerate_mus 的指数级分支，这里只做线性次求解，
    适合大规模系统快速给出"若干"冲突；可能漏掉与已禁用边重叠的 MUS。
    """
    sys, _ = _system_and_index(constraints)
    m = len(sys.constraints)
    idx_of = {c.cid: i for i, c in enumerate(sys.constraints)}
    forbidden: Set[int] = set()
    found: List[MinimalConflict] = []
    seen: Set[FrozenSet[int]] = set()
    calls = 0
    while len(found) < max_mus:
        keep = [i not in forbidden for i in range(m)]
        calls += 1
        res = sys.check(keep)
        if res.feasible:
            break
        cyc = frozenset(idx_of[c] for c in res.neg_cycle_cids)
        if cyc not in seen:
            seen.add(cyc)
            mc = _make_conflict(sys, sorted(cyc), res.neg_cycle_weight)
            if verify:
                ok, checks = verify_mus(sys, mc.cids)
                mc.verified_minimal = ok
                mc.verification = checks
            found.append(mc)
        forbidden.add(min(cyc))
    return found, calls


def extract_mus_deletion(constraints: Sequence[Constraint]
                         ) -> Optional[MinimalConflict]:
    """对照实现：经典删除过滤法（deletion-based MUS 提取）。

    从全集出发，逐条尝试删除，删完仍不可满足则真正删除。
    结果必为 MUS（标准结论），用于与负环法交叉验证。
    复杂度 O(m) 次求解，仅适合小规模对照。
    """
    sys, _ = _system_and_index(constraints)
    m = len(sys.constraints)
    current: Set[int] = set(range(m))
    if _subset_check(sys, m, frozenset(current)):
        return None
    for i in range(m):
        if i in current and not _subset_check(sys, m, frozenset(current - {i})):
            current.discard(i)
    cids = tuple(sorted(sys.constraints[i].cid for i in current))
    mc = MinimalConflict(cids=cids, size=len(cids), cycle_weight=0,
                         self_contradictory=(len(cids) == 1))
    ok, checks = verify_mus(sys, mc.cids)
    mc.verified_minimal = ok
    mc.verification = checks
    return mc


def brute_force_mus(constraints: Sequence[Constraint]) -> List[FrozenSet[str]]:
    """暴力枚举所有子集求全部 MUS（真值机，仅用于小规模自测）。"""
    cons = normalize_ids(constraints)
    sys = DifferenceSystem(max([max(c.u, c.v) for c in cons], default=0), cons)
    m = len(cons)
    unsat: List[FrozenSet[int]] = []
    for r in range(1, m + 1):
        for combo in combinations(range(m), r):
            if not _subset_check(sys, m, frozenset(combo)):
                unsat.append(frozenset(combo))
    mus = [s for s in unsat
           if not any(t < s for t in unsat)]  # 按包含关系极小
    return [frozenset(cons[i].cid for i in s) for s in mus]

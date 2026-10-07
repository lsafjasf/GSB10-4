"""演示文稿放映顺序推导库（Python 3 标准库实现）。

背景
----
演示文稿每页都有一个编号（来自文件名，如 slide7.xml 中的 7），
同时每页的关系文件声明了 next 引用，指向逻辑上的下一页。
编号顺序与关系顺序经常不一致，关系文件还可能引用不存在的对象。

推导规则（冲突时一律以关系为准，编号只作辅助）
------------------------------------------------
R1 真实放映顺序 = 从起始页沿 next 链依次遍历得到的页面序列。
R2 起始页 = 没有任何其他页引用它的页（入度为 0）。
    若存在多个起始页，按编号从小到大依次各走一条链，并报告 MULTIPLE_HEADS。
    若不存在起始页（全图成环），从编号最小的页进入，环会被检测并报告。
R3 编号仅用于：并列起始页的排序、对照基准、异常定位；绝不覆盖关系。
R4 next 指向不存在的页 -> DANGLING_REF，该边按"链结束"处理，不中断其余推导。
R5 next 指向自身 -> SELF_REF，该边按"链结束"处理。
R6 同一页被多页引用为 next -> DUPLICATE_REF，沿主链遍历自然生效一条，
    其余前驱所在的链段作为独立分量追加遍历，不丢页。
R7 遍历时回到本链已访问的页 -> CYCLE，报告环上全部节点并停止该链扩展，
    绝不无限递归（本库为纯迭代实现，递归深度上界为 1）。
R8 主链走完后仍未访问的页（孤立环、断链分量）按编号升序逐个分量补走，
    保证每一页都出现在最终顺序中，且每页只出现一次。

复杂度
------
设 n 为页数：时间 O(n)，额外空间 O(n)，遍历步数上界 n，
递归深度上界 1（无递归，纯迭代 while 循环）。
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# 异常分类
SELF_REF = "SELF_REF"              # 自引用
DANGLING_REF = "DANGLING_REF"      # 悬空引用
DUPLICATE_REF = "DUPLICATE_REF"    # 重复引用（同一页被多个前驱指向）
MULTIPLE_HEADS = "MULTIPLE_HEADS"  # 多个起始页
CYCLE = "CYCLE"                    # 引用成环


@dataclass(frozen=True)
class Slide:
    """一页演示文稿。

    slide_id: 文件编号（如 slide7.xml 的 7）。
    name:     页面对象名（如 "slide7.xml"）。
    next_ref: 关系文件中声明的下一页对象名；None 表示关系上无后继。
    """

    slide_id: int
    name: str
    next_ref: Optional[str] = None


@dataclass(frozen=True)
class Anomaly:
    """一条分类异常报告。"""

    kind: str
    slides: Tuple[str, ...]
    detail: str
    suggestion: str


@dataclass(frozen=True)
class PageMismatch:
    """推导顺序与编号顺序在某一页位上的不一致。"""

    position: int            # 1 起始的页位
    numeric: Optional[str]   # 该页位上按编号排序的页
    derived: Optional[str]   # 该页位上按关系推导的页
    reason: str


@dataclass
class DerivationResult:
    order: List[str]              # 关系推导出的真实放映顺序
    numeric_order: List[str]      # 按编号排序的对照顺序
    anomalies: List[Anomaly]      # 全部分类异常
    mismatches: List[PageMismatch]
    stats: Dict[str, object]


def derive_order(slides: Sequence[Slide]) -> DerivationResult:
    """按关系图推导真实放映顺序，并与编号顺序逐页对照。"""
    started = time.perf_counter()
    slides = list(slides)

    by_name: Dict[str, Slide] = {}
    for s in slides:
        if s.name in by_name:
            raise ValueError(f"重复的页对象名: {s.name}")
        by_name[s.name] = s

    anomalies: List[Anomaly] = []
    next_map: Dict[str, str] = {}
    predecessors: Dict[str, List[str]] = defaultdict(list)

    # --- 第一遍：建边并分类引用异常（R4/R5/R6） ---
    for s in slides:
        ref = s.next_ref
        if ref is None:
            continue
        if ref == s.name:
            anomalies.append(Anomaly(
                SELF_REF, (s.name,),
                f"{s.name} 的 next 引用指向自身",
                "删除该自引用；若本意是指向其他页，请修正 next 的目标对象名",
            ))
            continue
        if ref not in by_name:
            anomalies.append(Anomaly(
                DANGLING_REF, (s.name,),
                f"{s.name} 的 next 引用 {ref} 指向不存在的对象",
                f"二选一：删除 {s.name} 的 next 引用（该页作为链尾），"
                f"或补齐缺失的 {ref} 对象",
            ))
            continue
        next_map[s.name] = ref
        predecessors[ref].append(s.name)

    for target in sorted(predecessors, key=lambda n: by_name[n].slide_id):
        preds = sorted(predecessors[target])
        if len(preds) > 1:
            anomalies.append(Anomaly(
                DUPLICATE_REF, (target, *preds),
                f"{target} 被 {len(preds)} 页同时声明为 next: {', '.join(preds)}",
                f"保留主链上实际生效的一条引用，删除其余前驱页指向 {target} 的 next",
            ))

    def num(name: str) -> int:
        return by_name[name].slide_id

    # --- 起始页选择（R2） ---
    heads = sorted((s.name for s in slides if s.name not in predecessors), key=num)
    if len(heads) > 1:
        anomalies.append(Anomaly(
            MULTIPLE_HEADS, tuple(heads),
            f"存在 {len(heads)} 个起始页: {', '.join(heads)}",
            "为除真正首页外的起始页补上前驱引用，使全图只有一条主链",
        ))

    # --- 第二遍：迭代式沿链遍历（R1/R7/R8） ---
    order: List[str] = []
    visited: Dict[str, bool] = {}

    def walk(start: str) -> None:
        node: Optional[str] = start
        local_index: Dict[str, int] = {}
        path: List[str] = []
        while node is not None and node not in visited:
            local_index[node] = len(path)
            path.append(node)
            visited[node] = True
            node = next_map.get(node)
        if node is not None and node in local_index:
            cycle = tuple(path[local_index[node]:])
            anomalies.append(Anomaly(
                CYCLE, cycle,
                f"引用成环: {' -> '.join(cycle)} -> {cycle[0]}",
                f"断开环上任意一条边即可修复，建议删除 {cycle[-1]} "
                f"指向 {cycle[0]} 的 next 引用",
            ))
        order.extend(path)

    for head in heads:
        walk(head)
    if not order and slides:
        # 没有任何入度为 0 的页：全图成环，从编号最小页进入（R2）
        walk(min((s.name for s in slides), key=num))
    for name in sorted((s.name for s in slides), key=num):
        if name not in visited:
            walk(name)

    # --- 与编号顺序逐页对照（R3） ---
    numeric_order = [s.name for s in sorted(slides, key=lambda s: s.slide_id)]
    prev_of = {ref: src for src, ref in next_map.items()}
    mismatches: List[PageMismatch] = []
    for i in range(max(len(numeric_order), len(order))):
        n = numeric_order[i] if i < len(numeric_order) else None
        d = order[i] if i < len(order) else None
        if n == d:
            continue
        if d is None:
            reason = "关系链比编号序列短，该页位无推导页"
        else:
            prev = prev_of.get(d)
            if prev is not None:
                reason = f"关系链中 {prev} 的 next 指向 {d}，关系优先于编号"
            else:
                reason = f"{d} 是关系链的起始页，编号不代表放映先后"
        mismatches.append(PageMismatch(i + 1, n, d, reason))

    stats: Dict[str, object] = {
        "slide_count": len(slides),
        "elapsed_seconds": time.perf_counter() - started,
        "max_recursion_depth": 1,          # 纯迭代实现，无递归调用
        "walk_steps": len(order),          # 上界 = 页数 n
        "anomaly_count": len(anomalies),
    }
    return DerivationResult(order, numeric_order, anomalies, mismatches, stats)


def format_report(result: DerivationResult, max_mismatch_rows: int = 10) -> str:
    """把推导结果格式化为人类可读报告（异常分类 + 对照表）。"""
    lines: List[str] = []
    stats = result.stats
    lines.append(
        f"页数={stats['slide_count']}  耗时={stats['elapsed_seconds'] * 1000:.2f}ms  "
        f"递归深度上界={stats['max_recursion_depth']}  "
        f"遍历步数={stats['walk_steps']}"
    )
    lines.append("")
    if result.anomalies:
        lines.append(f"异常分类报告（共 {len(result.anomalies)} 条）:")
        for a in result.anomalies:
            lines.append(f"  [{a.kind}] {a.detail}")
            lines.append(f"    修复建议: {a.suggestion}")
    else:
        lines.append("异常分类报告: 无异常")
    lines.append("")
    if result.mismatches:
        lines.append(
            f"推导顺序 vs 编号顺序对照（{len(result.mismatches)} 个页位不一致）:"
        )
        lines.append(f"  {'页位':>4}  {'编号顺序':<14}  {'推导顺序':<14}  原因")
        for m in result.mismatches[:max_mismatch_rows]:
            lines.append(
                f"  {m.position:>4}  {str(m.numeric):<14}  {str(m.derived):<14}  {m.reason}"
            )
        if len(result.mismatches) > max_mismatch_rows:
            lines.append(f"  ... 其余 {len(result.mismatches) - max_mismatch_rows} 行省略")
    else:
        lines.append("推导顺序 vs 编号顺序对照: 完全一致")
    return "\n".join(lines)


def _demo() -> None:
    """演示：1) 500 页打乱顺序的性能用例；2) 含各类异常的小样例。"""
    import random

    print("=" * 72)
    print("用例 A: 500 页，关系顺序与编号完全打乱")
    print("=" * 72)
    rng = random.Random(42)
    perm = list(range(1, 501))
    rng.shuffle(perm)
    deck = [
        Slide(i, f"slide{i}.xml",
              f"slide{perm[k + 1]}.xml" if k + 1 < len(perm) else None)
        for k, i in enumerate(perm)
    ]
    print(format_report(derive_order(deck), max_mismatch_rows=5))

    print()
    print("=" * 72)
    print("用例 B: 8 页，覆盖悬空/重复/自引用/成环/顺序冲突")
    print("=" * 72)
    messy = [
        Slide(1, "slide1.xml", "slide3.xml"),    # 编号与顺序打架：1 -> 3
        Slide(2, "slide2.xml", "slide1.xml"),    # 首页其实是 slide2
        Slide(3, "slide3.xml", "slide99.xml"),   # 悬空引用
        Slide(4, "slide4.xml", "slide4.xml"),    # 自引用
        Slide(5, "slide5.xml", "slide6.xml"),
        Slide(6, "slide6.xml", "slide7.xml"),
        Slide(7, "slide7.xml", "slide5.xml"),    # 5->6->7->5 成环
        Slide(8, "slide8.xml", "slide6.xml"),    # 重复引用 slide6
    ]
    print(format_report(derive_order(messy)))


if __name__ == "__main__":
    _demo()

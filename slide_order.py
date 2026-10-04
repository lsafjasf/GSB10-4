"""演示文稿放映顺序推导库（仅标准库）。

关系文件（JSON）格式::

    {
      "start": 3,                         # 可选；缺省时取"入度为 0 且编号最小"的页
      "slides": [1, 2, 3, 4],             # 可选；声明真实存在的页编号
      "relations": {                      # 页编号 -> 后继页编号列表（一般只有 1 个）
        "3": [1], "1": [2], "2": [4], "4": []
      }
    }

编号（文件号 / slides 中的数字）只决定"按编号排序"的对照序列；
放映顺序以 relations 构成的关系图为准，全部遍历均为迭代实现，
不使用递归，因此不存在递归深度问题（调用栈深度恒为 O(1)）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---- 异常分类常量 ---------------------------------------------------------
DANGLING = "dangling_reference"    # 悬空引用：指向不存在的页
DUPLICATE = "duplicate_reference"  # 重复引用：同一后继出现多次，或被多个前驱引用
SELF_REF = "self_reference"        # 自引用：页指向自己
CYCLE = "cycle"                    # 成环：沿后继边回到已访问节点
UNREACHABLE = "unreachable_slide"  # 补充：从起点不可达的孤立页


@dataclass
class Anomaly:
    """一条异常。kind 取上面的分类常量。"""

    kind: str
    nodes: List[Any]
    message: str
    suggestion: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "nodes": self.nodes,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass
class CompareRow:
    slide: Any
    numbered_position: Optional[int]          # 按编号排序的位置（从 1 开始）
    derived_position: Optional[int]           # 关系图推导位置；不可达为 None
    status: str                                # match / mismatch
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "slide": self.slide,
            "numbered_position": self.numbered_position,
            "derived_position": self.derived_position,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class AnalysisResult:
    order: List[Any] = field(default_factory=list)
    numbered_order: List[Any] = field(default_factory=list)
    anomalies: List[Anomaly] = field(default_factory=list)
    cycles: List[List[Any]] = field(default_factory=list)
    unreachable: List[Any] = field(default_factory=list)
    comparison: List[CompareRow] = field(default_factory=list)
    elapsed_ms: float = 0.0
    max_chain_depth: int = 0   # 最长遍历链长度（迭代步数，非调用栈深度）

    @property
    def call_stack_depth_bound(self) -> int:
        """算法全程迭代，不递归：函数调用栈深度上界为 0（与页数无关）。"""
        return 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "order": self.order,
            "numbered_order": self.numbered_order,
            "anomalies": [a.as_dict() for a in self.anomalies],
            "cycles": self.cycles,
            "unreachable": self.unreachable,
            "comparison": [c.as_dict() for c in self.comparison],
            "elapsed_ms": round(self.elapsed_ms, 6),
            "max_chain_depth": self.max_chain_depth,
            "call_stack_depth_bound": self.call_stack_depth_bound,
        }


# ---- 加载与归一化 ---------------------------------------------------------

def load_relations(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize(spec: Dict[str, Any]) -> Tuple[List[Any], Dict[Any, List[Any]], Any]:
    """归一化为 (全部页编号, 页->后继列表, 起点)。

    slides 缺省时，页集合取 relations 的全部键；没有出现在 relations 中
    的目标不自动算作存在——那正是悬空引用。
    """
    if "slides" in spec:
        slides = list(spec["slides"])
    else:
        slides = list(spec.get("relations", {}).keys())

    raw_relations: Dict[str, Any] = spec.get("relations", {})
    relations: Dict[Any, List[Any]] = {}
    for slide in slides:
        nxt = raw_relations.get(str(slide), raw_relations.get(slide, []))
        if nxt is None:
            nxt = []
        relations[slide] = list(nxt)

    start = spec.get("start")
    if start is None:
        # 自引用不算入度：否则自环节点永远不会被选为起点。
        referenced = {
            t for source, targets in relations.items() for t in targets if t != source
        }
        heads = [s for s in slides if s not in referenced]
        start = min(heads) if heads else min(slides)
    return slides, relations, start


# ---- 核心推导 -------------------------------------------------------------

def analyze(spec: Dict[str, Any]) -> AnalysisResult:
    """按关系图推导放映顺序并做全部检查。"""
    begin = time.perf_counter()

    slides, relations, start = normalize(spec)
    result = AnalysisResult()

    # 1) 结构性异常：自引用、悬空、重复（后继列表内重复 + 多个前驱指向同一页）。
    incoming: Dict[Any, List[Any]] = {s: [] for s in slides}
    for source, targets in relations.items():
        seen_here: set = set()
        for target in targets:
            if target == source:
                result.anomalies.append(Anomaly(
                    SELF_REF, [source],
                    f"页 {source} 的后继指向自身，形成 1 节点自环。",
                    f"删除页 {source} 的自引用边，或把它改指到真正的下一页。",
                ))
            elif target not in relations:
                result.anomalies.append(Anomaly(
                    DANGLING, [source, target],
                    f"页 {source} 引用了不存在的页 {target}（悬空引用）。",
                    f"三选一：删除边 {source}->{target}；将目标改指到存在的页；"
                    f"或在 slides 中补建缺失的页 {target}。",
                ))
            else:
                if target in seen_here:
                    result.anomalies.append(Anomaly(
                        DUPLICATE, [source, target],
                        f"页 {source} 的后继列表中页 {target} 重复出现。",
                        f"在页 {source} 的后继列表中只保留第一个 {target}，删除其余。",
                    ))
                else:
                    incoming.setdefault(target, []).append(source)
                seen_here.add(target)

    for target, sources in incoming.items():
        real_sources = [s for s in sources if s != target]
        if len(real_sources) > 1:
            result.anomalies.append(Anomaly(
                DUPLICATE, real_sources + [target],
                f"页 {target} 被多个前驱引用：{', '.join(map(str, real_sources))}。",
                f"只保留关系链上先到达的前驱对 {target} 的引用，删除其余边。",
            ))

    # 2) 从起点迭代遍历。每个节点只取第一条"有效后继"（存在且非自身），
    #    其余边已在第 1 步作为异常报告，不忽略；已访问节点不跳过，
    #    交给循环顶部的成环检测处理，保证环一定被指出而不是被绕开。
    position: Dict[Any, int] = {}
    path: List[Any] = []
    on_path: set = set()
    current: Any = start if start in relations else None
    if current is None:
        result.anomalies.append(Anomaly(
            DANGLING, [start],
            f"起点页 {start} 不存在，无法开始放映。",
            f"把 start 改为存在的页，或补建页 {start}。",
        ))

    while current is not None:
        if current in on_path:
            idx = position[current]
            cycle = path[idx:]
            result.cycles.append(cycle)
            result.anomalies.append(Anomaly(
                CYCLE, cycle,
                f"关系图成环：{' -> '.join(map(str, cycle))} -> {current}，"
                f"环上共 {len(cycle)} 个节点。",
                f"断开环上一条回边即可线性化，例如删除 {cycle[-1]}->{cycle[0]}。",
            ))
            break
        position[current] = len(path)
        path.append(current)
        on_path.add(current)

        nxt_node: Any = None
        for candidate in relations.get(current, []):
            if candidate in relations and candidate != current:
                nxt_node = candidate
                break
        current = nxt_node

    result.order = path
    result.max_chain_depth = len(path)

    # 3) 不可达页：存在但不在主遍历路径上（包括孤立环 / 孤立链上的页）。
    reachable = set(path)
    result.unreachable = [s for s in slides if s not in reachable]
    if result.unreachable:
        result.anomalies.append(Anomaly(
            UNREACHABLE, result.unreachable,
            f"以下页从起点 {start} 不可达，不会被放映："
            f"{', '.join(map(str, result.unreachable))}。",
            "在主链上为这些页补一条后继边把它们接入，或确认后从演示文稿中移除。",
        ))

    # 4) 在不可达区域内继续找环（孤立环也要全部指出）。
    main_cycles = len(result.cycles)
    result.cycles.extend(_find_remaining_cycles(relations, reachable))
    for cycle in result.cycles[main_cycles:]:
        result.anomalies.append(Anomaly(
            CYCLE, cycle,
            f"不可达区域内存在环：{' -> '.join(map(str, cycle))} -> {cycle[0]}，"
            f"环上共 {len(cycle)} 个节点。",
            f"先把该环接入主链，再删除回边（如 {cycle[-1]}->{cycle[0]}）线性化。",
        ))

    # 5) 与按编号排序逐页对照。
    result.numbered_order = sorted(slides)
    result.comparison = _compare(slides, result.numbered_order, result.order,
                                 result.cycles, reachable)

    result.elapsed_ms = (time.perf_counter() - begin) * 1000.0
    return result


def _find_remaining_cycles(
    relations: Dict[Any, List[Any]], reachable: set
) -> List[List[Any]]:
    """在尚未到达的子图中迭代找环，不使用递归。"""
    cycles: List[List[Any]] = []
    visited: set = set(reachable)
    for node in relations:
        if node in visited:
            continue
        path: List[Any] = []
        on_path: Dict[Any, int] = {}
        cur = node
        while cur is not None and cur in relations and cur not in visited:
            if cur in on_path:
                cycles.append(path[on_path[cur]:])
                break
            on_path[cur] = len(path)
            path.append(cur)
            nxt = None
            for cand in relations[cur]:
                if cand in relations and cand != cur:
                    nxt = cand
                    break
            cur = nxt
        visited.update(path)
    return cycles


def _compare(
    slides: List[Any],
    numbered_order: List[Any],
    order: List[Any],
    cycles: List[List[Any]],
    reachable: set,
) -> List[CompareRow]:
    derived_pos = {s: i + 1 for i, s in enumerate(order)}
    cycle_nodes = {n for cyc in cycles for n in cyc}
    rows: List[CompareRow] = []
    for pos_num, slide in enumerate(numbered_order, start=1):
        pos_rel = derived_pos.get(slide)
        if pos_rel == pos_num:
            rows.append(CompareRow(slide, pos_num, pos_rel, "match",
                                   "编号位置与关系图推导位置一致。"))
            continue
        if slide in cycle_nodes:
            reason = "位于关系环上，遍历在入环处截断，无法给出线性位置。"
        elif slide not in reachable:
            reason = "从起点不可达，按编号排序会出现，但放映序列中不存在该页。"
        elif pos_rel is None:
            reason = "放映序列中缺失（被异常边截断）。"
        else:
            reason = (f"编号排序在第 {pos_num} 位，但关系边使其出现在第 {pos_rel} 位；"
                      "编号与顺序打架，以关系图为准。")
        rows.append(CompareRow(slide, pos_num, pos_rel, "mismatch", reason))
    return rows


# ---- 报告格式化 -----------------------------------------------------------

_KIND_LABEL = {
    DANGLING: "悬空引用",
    DUPLICATE: "重复引用",
    SELF_REF: "自引用",
    CYCLE: "关系成环",
    UNREACHABLE: "不可达页",
}


def format_report(result: AnalysisResult, name: str = "") -> str:
    lines: List[str] = []
    title = f"放映顺序推导报告 {('— ' + name) if name else ''}"
    lines.append(title)
    lines.append("=" * max(36, len(title)))
    lines.append(f"按编号排序：{result.numbered_order}")
    lines.append(f"关系图推导：{result.order}")
    lines.append(f"遍历链最长步数：{result.max_chain_depth}（迭代实现，"
                 f"函数调用栈深度上界 {result.call_stack_depth_bound}，与页数无关）")
    lines.append(f"解析耗时：{result.elapsed_ms:.4f} ms")

    lines.append("")
    lines.append("逐页对照（仅列不一致页）")
    lines.append("-" * 36)
    mismatches = [c for c in result.comparison if c.status == "mismatch"]
    if not mismatches:
        lines.append("无不一致：关系图推导与按编号排序完全一致。")
    for row in mismatches:
        lines.append(
            f"页 {row.slide}: 编号位 {row.numbered_position} / 推导位 "
            f"{row.derived_position if row.derived_position is not None else '缺失'}"
            f" —— {row.reason}"
        )

    lines.append("")
    lines.append("异常分类报告与修复建议")
    lines.append("-" * 36)
    if not result.anomalies:
        lines.append("未发现异常。")
    for index, anomaly in enumerate(result.anomalies, start=1):
        lines.append(f"[{index}] {_KIND_LABEL[anomaly.kind]}（{anomaly.kind}）")
        lines.append(f"    现象：{anomaly.message}")
        lines.append(f"    建议：{anomaly.suggestion}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="按关系图推导演示文稿放映顺序")
    parser.add_argument("relations", help="关系文件 JSON 路径")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    spec = load_relations(args.relations)
    result = analyze(spec)
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(result, args.relations))
    return 0


if __name__ == "__main__":
    sys.exit(main())

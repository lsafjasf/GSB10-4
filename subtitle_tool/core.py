"""字幕合并与偏移核心库（仅标准库）。

时间模型
--------
- 所有时间均为整数毫秒。
- 区间一律按半开区间 [start, end) 处理：end == 下一条 start 不算重叠。
- 合法字幕必须满足 0 <= start < end（时长 >= 1ms）。

合并规则（merge_sources）
------------------------
1. 所有来源的条目按 (start, -duration, 来源顺序) 排序后依次处理：
   开始时间早的优先；开始时间相同则时长长的优先；区间完全相同则先出现的来源优先。
2. 已接受区间与待处理区间重叠时，待处理区间被“从左侧裁剪”（截掉重叠
   前缀，保留剩余部分）；因处理顺序按 start 升序，裁剪只发生在左侧，
   条目不会被分裂成多段。
   - 完全被覆盖：整条丢弃，记入报告（kind="dropped_fully_covered"）。
3. 裁剪后的条目继承原条目的文本与来源标签。
4. 合并结果按开始时间升序，保证无嵌套、无重叠、无零时长条目。

偏移规则（shift_global / shift_segmented）
----------------------------------------
- 严格模式（默认 strict=True）：任何条目偏移后 start < 0 都记入报告
  （kind="start_before_zero"）并保持原样，绝不在就地截断为 0。
- 分段偏移：时间窗按 start 升序给出且互不重叠；落在窗口内的条目应用该窗口
  的偏移，窗口外条目保持不变。窗口外的负偏移不会把条目移出窗口——偏移只改
  时间戳，不改变条目归属。
- 分段偏移可能制造新的重叠（不同窗口偏移量不同），这些重叠会被检测并记入
  报告（kind="overlap_introduced"），由调用方决定是否再合并。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

# 报告类型常量
DROPPED_FULLY_COVERED = "dropped_fully_covered"
START_BEFORE_ZERO = "start_before_zero"
OVERLAP_INTRODUCED = "overlap_introduced"


@dataclass(frozen=True)
class Cue:
    """一条字幕。[start, end) 半开区间，单位毫秒。"""

    start: int
    end: int
    text: str
    tag: str = ""  # 来源标签（如文件名），便于追溯

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError(f"start 不能为负: {self.start}")
        if self.end <= self.start:
            raise ValueError(f"零时长或负时长条目: [{self.start}, {self.end})")

    @property
    def duration(self) -> int:
        return self.end - self.start

    def shifted(self, offset_ms: int) -> "Cue":
        return Cue(self.start + offset_ms, self.end + offset_ms, self.text, self.tag)


@dataclass(frozen=True)
class ShiftWindow:
    """分段偏移窗口：落在 [start, end) 内的条目整体平移 offset_ms。"""

    start: int
    end: int
    offset_ms: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError(f"非法窗口: [{self.start}, {self.end})")


@dataclass(frozen=True)
class Report:
    """越界 / 丢弃 / 新重叠报告条目。"""

    kind: str
    cue: Cue
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "cue": {"start": self.cue.start, "end": self.cue.end,
                    "text": self.cue.text, "tag": self.cue.tag},
            "detail": self.detail,
        }


@dataclass
class MergeResult:
    cues: List[Cue]
    reports: List[Report] = field(default_factory=list)


@dataclass
class ShiftResult:
    cues: List[Cue]
    reports: List[Report] = field(default_factory=list)


class ShiftViolationError(ValueError):
    """strict 模式下存在越界条目时抛出，reports 属性携带全部报告。"""

    def __init__(self, reports: List[Report]):
        self.reports = list(reports)
        super().__init__(f"{len(reports)} 条字幕偏移后越界（start < 0）")


# ---------------------------------------------------------------- 合并

def merge_sources(sources: Sequence[Sequence[Cue]]) -> MergeResult:
    """合并多个来源的字幕，规则见模块 docstring。

    处理顺序按 start 升序，已接受区间的 start 都不超过当前条目，因此
    当前条目只会被“从左侧裁剪”（截掉与最后一个已接受区间重叠的前缀），
    不会被分裂成多段；完全被覆盖则丢弃并记入报告。
    """
    order: List[Tuple[Cue, int]] = []
    for src_idx, cues in enumerate(sources):
        for cue in cues:
            order.append((cue, src_idx))
    # 开始时间升序；同开始则时长降序；再按来源顺序保证确定性
    order.sort(key=lambda item: (item[0].start, -item[0].duration, item[1]))

    accepted: List[Cue] = []
    reports: List[Report] = []
    for cue, _src_idx in order:
        start = cue.start
        if accepted and accepted[-1].end > start:
            blocker = accepted[-1]
            if blocker.end >= cue.end:
                reports.append(Report(
                    DROPPED_FULLY_COVERED, cue,
                    f"被 [{blocker.start}, {blocker.end}) 完全覆盖"))
                continue
            start = blocker.end  # 从左侧裁剪
        accepted.append(Cue(start, cue.end, cue.text, cue.tag))
    return MergeResult(accepted, reports)


# ---------------------------------------------------------------- 偏移

def shift_global(cues: Sequence[Cue], offset_ms: int,
                 strict: bool = True) -> ShiftResult:
    """整段平移。越界（start+offset<0）条目记入报告并保持原样。"""
    out: List[Cue] = []
    reports: List[Report] = []
    for cue in cues:
        if cue.start + offset_ms < 0:
            reports.append(Report(
                START_BEFORE_ZERO, cue,
                f"offset={offset_ms}ms 使 start={cue.start} -> {cue.start + offset_ms}"))
            out.append(cue)  # 不截断，保持原样
        else:
            out.append(cue.shifted(offset_ms))
    if strict and reports:
        raise ShiftViolationError(reports)
    return ShiftResult(out, reports)


def _validate_windows(windows: Sequence[ShiftWindow]) -> None:
    prev_end = -1
    for w in windows:
        if w.start < prev_end:
            raise ValueError(f"偏移窗口重叠或乱序: [{w.start}, {w.end})")
        prev_end = w.end


def _detect_overlaps(cues: Sequence[Cue]) -> List[Tuple[Cue, Cue]]:
    """检测已按 start 排序的条目中的重叠对。"""
    pairs: List[Tuple[Cue, Cue]] = []
    for i in range(len(cues) - 1):
        if cues[i].end > cues[i + 1].start:
            pairs.append((cues[i], cues[i + 1]))
    return pairs


def shift_segmented(cues: Sequence[Cue], windows: Sequence[ShiftWindow],
                    strict: bool = True) -> ShiftResult:
    """按区间分段平移。

    窗口按 start 升序且互不重叠。窗口内条目应用窗口偏移，窗口外不变。
    返回结果按 start 升序；偏移引入的新重叠记入报告。
    """
    _validate_windows(windows)
    wstarts = [w.start for w in windows]
    out: List[Cue] = []
    reports: List[Report] = []
    for cue in cues:
        idx = bisect.bisect_right(wstarts, cue.start) - 1
        offset = 0
        if idx >= 0 and cue.start < windows[idx].end:
            offset = windows[idx].offset_ms
        if cue.start + offset < 0:
            reports.append(Report(
                START_BEFORE_ZERO, cue,
                f"窗口偏移 {offset}ms 使 start={cue.start} -> {cue.start + offset}"))
            out.append(cue)
        else:
            out.append(cue.shifted(offset))
    out.sort(key=lambda c: (c.start, c.end))
    for a, b in _detect_overlaps(out):
        reports.append(Report(
            OVERLAP_INTRODUCED, b,
            f"与 [{a.start}, {a.end}) 重叠（分段偏移引入）"))
    if strict and any(r.kind == START_BEFORE_ZERO for r in reports):
        raise ShiftViolationError(
            [r for r in reports if r.kind == START_BEFORE_ZERO])
    return ShiftResult(out, reports)

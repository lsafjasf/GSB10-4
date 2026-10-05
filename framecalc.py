"""窗口函数帧边界计算库（仅使用 Python 标准库）。

帧语义（与 SQL 标准对齐，详见 README.md）：
- 分区内的行先按 (排序键, 原始行号) 升序排列，形成确定的全序；
  排序键相同（并列）时按原始行号升序，保证结果可复现。
- 帧表示为有序分区上的闭区间 [lo, hi]（两端均含）。lo > hi 表示空帧。
- ROWS  : 按物理行偏移。帧可能从并列组中间切开（截断）。
- RANGE : 按排序键的值偏移（数值键）。并列行（peer）要么全部入帧，
          要么全部出帧（并入），帧边界永远不会切开并列组。
- GROUPS: 按并列组（peer group）偏移，同样不会切开并列组。
- 空帧约定：sum/min/max 为 None（对应 SQL 的 NULL），count 为 0。

帧返回值：compute_frames 返回 (los, his)，两个等长的 array('i')，
第 i 个元素给出排序后第 i 行的帧闭区间 [los[i], his[i]]。
"""

from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "BoundType", "Bound", "FrameMode", "FrameSpec",
    "compute_frames", "window_aggregate", "sort_order",
]


class BoundType(Enum):
    UNBOUNDED_PRECEDING = 0
    PRECEDING = 1
    CURRENT_ROW = 2
    FOLLOWING = 3
    UNBOUNDED_FOLLOWING = 4


@dataclass(frozen=True)
class Bound:
    type: BoundType
    offset: int = 0  # 仅 PRECEDING / FOLLOWING 有效，必须为非负整数

    def __post_init__(self):
        if self.type in (BoundType.PRECEDING, BoundType.FOLLOWING):
            if not isinstance(self.offset, int) or self.offset < 0:
                raise ValueError("PRECEDING/FOLLOWING 的 offset 必须是非负整数")
        elif self.offset != 0:
            raise ValueError("UNBOUNDED/CURRENT ROW 不允许带 offset")


class FrameMode(Enum):
    ROWS = "ROWS"
    RANGE = "RANGE"
    GROUPS = "GROUPS"


@dataclass(frozen=True)
class FrameSpec:
    mode: FrameMode
    start: Bound
    end: Bound

    def __post_init__(self):
        # 与 SQL 标准一致：
        # 1) 帧起点在边界序上不得晚于帧终点；
        # 2) 起点不能是 UNBOUNDED FOLLOWING，终点不能是 UNBOUNDED PRECEDING。
        if self.start.type.value > self.end.type.value:
            raise ValueError(
                "帧起点不能晚于帧终点: %s .. %s" % (self.start, self.end)
            )
        if self.start.type is BoundType.UNBOUNDED_FOLLOWING:
            raise ValueError("帧起点不能是 UNBOUNDED FOLLOWING")
        if self.end.type is BoundType.UNBOUNDED_PRECEDING:
            raise ValueError("帧终点不能是 UNBOUNDED PRECEDING")


def sort_order(keys):
    """返回分区内确定性的行排列：按 (key, 原始下标) 升序。

    Python 的 sorted 是稳定排序，按 key 排序后并列行保持原始下标顺序，
    等价于全序 (key, index)，因此排序键不稳定（有重复）时结果仍可复现。
    """
    return sorted(range(len(keys)), key=keys.__getitem__)


def _empty_bounds(n):
    return array("i", [0]) * n, array("i", [0]) * n


def _rows_frames(spec, n, los, his):
    sk = spec.start.type
    ek = spec.end.type
    so = spec.start.offset
    eo = spec.end.offset
    last = n - 1
    for i in range(n):
        if sk is BoundType.UNBOUNDED_PRECEDING:
            lo = 0
        elif sk is BoundType.CURRENT_ROW:
            lo = i
        elif sk is BoundType.PRECEDING:
            raw = i - so
            lo = raw if raw > 0 else 0
        else:  # FOLLOWING：向上不截断，保留越界值以产生空帧
            lo = i + so
        if ek is BoundType.UNBOUNDED_FOLLOWING:
            hi = last
        elif ek is BoundType.CURRENT_ROW:
            hi = i
        elif ek is BoundType.FOLLOWING:
            raw = i + eo
            hi = raw if raw < last else last
        else:  # PRECEDING：向下不截断
            hi = i - eo
        los[i] = lo
        his[i] = hi


def _range_frames(spec, keys, n, los, his):
    sk = spec.start.type
    ek = spec.end.type
    so = spec.start.offset
    eo = spec.end.offset
    for i in range(n):
        k = keys[i]
        if sk is BoundType.UNBOUNDED_PRECEDING:
            lo = 0
        elif sk is BoundType.CURRENT_ROW:
            lo = bisect_left(keys, k)
        elif sk is BoundType.PRECEDING:
            lo = bisect_left(keys, k - so)
        else:  # FOLLOWING
            lo = bisect_left(keys, k + so)
        if ek is BoundType.UNBOUNDED_FOLLOWING:
            hi = n - 1
        elif ek is BoundType.CURRENT_ROW:
            hi = bisect_right(keys, k) - 1
        elif ek is BoundType.FOLLOWING:
            hi = bisect_right(keys, k + eo) - 1
        else:  # PRECEDING
            hi = bisect_right(keys, k - eo) - 1
        los[i] = lo
        his[i] = hi


def _groups_frames(spec, keys, n, los, his):
    group_of = array("i", [0]) * n
    group_start = []
    group_end = []
    g = -1
    for i in range(n):
        if i == 0 or keys[i] != keys[i - 1]:
            g += 1
            group_start.append(i)
            group_end.append(i)
        else:
            group_end[g] = i
        group_of[i] = g
    num_groups = g + 1
    sk = spec.start.type
    ek = spec.end.type
    so = spec.start.offset
    eo = spec.end.offset
    for i in range(n):
        gi = group_of[i]
        if sk is BoundType.UNBOUNDED_PRECEDING:
            lg = 0
        elif sk is BoundType.CURRENT_ROW:
            lg = gi
        elif sk is BoundType.PRECEDING:
            lg = gi - so
        else:  # FOLLOWING
            lg = gi + so
        if ek is BoundType.UNBOUNDED_FOLLOWING:
            hg = num_groups - 1
        elif ek is BoundType.CURRENT_ROW:
            hg = gi
        elif ek is BoundType.FOLLOWING:
            hg = gi + eo
        else:  # PRECEDING
            hg = gi - eo
        if 0 <= lg < num_groups:
            lo = group_start[lg]
        else:
            lo = 0 if lg < 0 else n
        if 0 <= hg < num_groups:
            hi = group_end[hg]
        else:
            hi = -1 if hg < 0 else n - 1
        los[i] = lo
        his[i] = hi


def compute_frames(spec, sorted_keys):
    """对已按 sort_order 排序的键序列，返回每行的帧边界 (los, his)。

    第 i 行的帧为排序后序列上的闭区间 [los[i], his[i]]；
    los[i] > his[i] 表示空帧。
    """
    n = len(sorted_keys)
    los, his = _empty_bounds(n)
    if n == 0:
        return los, his
    if spec.mode is FrameMode.ROWS:
        _rows_frames(spec, n, los, his)
    elif spec.mode is FrameMode.RANGE:
        _range_frames(spec, sorted_keys, n, los, his)
    else:
        _groups_frames(spec, sorted_keys, n, los, his)
    return los, his


def window_aggregate(spec, keys, values, agg="sum"):
    """对每一行计算帧内聚合，返回按原始行号排列的结果列表。

    keys/values 按原始行号给出；函数内部自行排序。
    空帧：sum/min/max -> None，count -> 0。
    """
    n = len(keys)
    if n != len(values):
        raise ValueError("keys 与 values 长度不一致")
    order = sort_order(keys)
    skeys = [keys[i] for i in order]
    svals = [values[i] for i in order]
    los, his = compute_frames(spec, skeys)

    if agg == "count":
        out_sorted = [
            0 if los[p] > his[p] else his[p] - los[p] + 1
            for p in range(n)
        ]
    elif agg == "sum":
        prefix = [0]
        append = prefix.append
        for v in svals:
            append(prefix[-1] + v)
        out_sorted = [
            None if los[p] > his[p] else prefix[his[p] + 1] - prefix[los[p]]
            for p in range(n)
        ]
    elif agg in ("min", "max"):
        pick = min if agg == "min" else max
        out_sorted = [
            None if los[p] > his[p] else pick(svals[los[p]:his[p] + 1])
            for p in range(n)
        ]
    else:
        raise ValueError("不支持的聚合: %r" % agg)

    out = [None] * n
    for p, idx in enumerate(order):
        out[idx] = out_sorted[p]
    return out

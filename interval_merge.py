"""时间区间重叠合并库（仅使用 Python 3 标准库）。

语义约定（详见 RULES.md 的规则表）：

1. 区间为闭区间 [start, end]，内部统一表示为 UTC 微秒整数。
2. 相接（一个区间的 end 恰好等于另一个区间的 start）算重叠，会被合并。
3. 完全包含时，被包含区间被吸收。
4. 零长度区间 [a, a] 是合法的“点区间”，参与合并。
5. 负长度区间（start > end）非法，默认报错；也可按策略丢弃或交换端点。
6. 带时区的时间戳一律转 UTC；无时区的按 default_tz（默认 UTC）解释。
7. 精度不一致时先按量子（quantum，微秒）对齐：start 向下取整、end 向上取整，
   保证不丢覆盖范围。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, List, NamedTuple, Sequence, Union

US_PER_SECOND = 1_000_000
US_PER_MS = 1_000


class NegativeLengthError(ValueError):
    """负长度区间（start > end），在默认策略下抛出。"""


@dataclass(frozen=True)
class Interval:
    """闭区间，端点为 UTC 微秒整数。end == start 合法（零长度点区间）。"""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise NegativeLengthError(
                f"负长度区间: start={self.start} > end={self.end}（区间端点不会被自动交换）"
            )

    @property
    def length(self) -> int:
        """以微秒计的长度，零长度区间返回 0。"""
        return self.end - self.start

    @property
    def is_zero_length(self) -> bool:
        return self.end == self.start


# --------------------------------------------------------------------------- #
# 输入归一化
# --------------------------------------------------------------------------- #

RawInput = Union[Interval, tuple, list, datetime, str, int, float]


def parse_time(value: RawInput, unit: str = "s",
               default_tz=timezone.utc) -> int:
    """把各种输入转成 UTC 微秒整数。

    - datetime：带时区则转 UTC；naive 视为 default_tz。
    - str：ISO 8601（支持结尾 Z）。
    - int/float：Unix 纪元值，unit 取 "s" / "ms" / "us"。
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    elif isinstance(value, (int, float)):
        if unit == "s":
            return int(round(value * US_PER_SECOND))
        if unit == "ms":
            return int(round(value * US_PER_MS))
        if unit == "us":
            return int(round(value))
        raise ValueError(f"未知时间单位: {unit!r}（可选 's'/'ms'/'us'）")
    else:
        raise TypeError(f"不支持的时间类型: {type(value).__name__}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return int(dt.astimezone(timezone.utc).timestamp() * US_PER_SECOND)


def make_interval(start: RawInput, end: RawInput, *, unit: str = "s",
                  default_tz=timezone.utc,
                  on_negative: str = "raise") -> Interval:
    """由原始端点构造区间。

    on_negative:
      - "raise"：负长度直接抛 NegativeLengthError（默认）
      - "swap" ：交换端点（仅用于容忍脏数据，结果会被标记）
      - "drop" ：返回 None，由调用方计数
    """
    s = parse_time(start, unit=unit, default_tz=default_tz)
    e = parse_time(end, unit=unit, default_tz=default_tz)
    if s > e:
        if on_negative == "raise":
            raise NegativeLengthError(
                f"负长度区间: {start!r} > {end!r}（原始端点不会被自动交换）"
            )
        if on_negative == "swap":
            return Interval(e, s)
        if on_negative == "drop":
            return None  # type: ignore[return-value]
        raise ValueError(f"未知 on_negative 策略: {on_negative!r}")
    return Interval(s, e)


def quantize(iv: Interval, quantum_us: int) -> Interval:
    """把区间对齐到 quantum_us 网格：start 向下取整，end 向上取整。

    对齐后区间只可能变“宽”，不会丢失原本覆盖的任何时刻（闭区间安全）。
    quantum_us 必须为正整数。
    """
    if quantum_us <= 0:
        raise ValueError("quantum_us 必须为正整数")
    s = iv.start - iv.start % quantum_us
    e = iv.end + (-iv.end % quantum_us)
    return Interval(s, e)


# --------------------------------------------------------------------------- #
# 合并核心
# --------------------------------------------------------------------------- #

def overlaps_or_touches(a: Interval, b: Interval) -> bool:
    """闭区间相交或恰好相接（end == start）即返回 True。"""
    return a.start <= b.end and b.start <= a.end


def merge_two(a: Interval, b: Interval) -> Interval:
    """合并两个已知相接/重叠的区间。"""
    return Interval(min(a.start, b.start), max(a.end, b.end))


def merge_sorted(intervals: Iterable[Interval]) -> List[Interval]:
    """O(n log n) 排序贪心合并（生产实现）。

    结果按 start 升序，相邻结果之间严格分离（prev.end < next.start）。
    """
    ordered = sorted(intervals, key=lambda iv: (iv.start, iv.end))
    result: List[Interval] = []
    for iv in ordered:
        if result and iv.start <= result[-1].end:  # 相接（==）也合并
            result[-1] = Interval(result[-1].start,
                                  max(result[-1].end, iv.end))
        else:
            result.append(iv)
    return result


def merge_pairwise(intervals: Sequence[Interval]) -> List[Interval]:
    """逐对两两合并的参考实现：不断找一对可合并区间并替换，直到没有可合并对。

    朴素 O(n^3)，仅用于对拍，结果同样排序归一化。
    """
    pool: List[Interval] = list(intervals)
    changed = True
    while changed:
        changed = False
        for i in range(len(pool)):
            for j in range(i + 1, len(pool)):
                if overlaps_or_touches(pool[i], pool[j]):
                    merged = merge_two(pool[i], pool[j])
                    pool = [x for k, x in enumerate(pool)
                            if k != i and k != j] + [merged]
                    changed = True
                    break
            if changed:
                break
    return sorted(pool, key=lambda iv: (iv.start, iv.end))


# --------------------------------------------------------------------------- #
# 高层接口
# --------------------------------------------------------------------------- #

class MergeReport(NamedTuple):
    merged: List[Interval]
    dropped_negative: List[tuple]  # 被丢弃的原始负长度区间
    swapped_negative: List[tuple]  # 被交换端点修正的原始负长度区间
    before_quantize: List[Interval]


def merge_inputs(raw_intervals: Iterable, *, unit: str = "s",
                 default_tz=timezone.utc,
                 on_negative: str = "raise",
                 quantum_us: int | None = None) -> MergeReport:
    """端到端：解析 -> 负长度处理 -> 精度对齐 -> 合并。

    raw_intervals 的每个元素为 (start, end)，端点支持
    ISO 8601 字符串 / datetime / 纪元数值（配合 unit）。
    """
    intervals: List[Interval] = []
    dropped: List[tuple] = []
    swapped: List[tuple] = []

    for raw in raw_intervals:
        start, end = raw[0], raw[1]
        s_us = parse_time(start, unit=unit, default_tz=default_tz)
        e_us = parse_time(end, unit=unit, default_tz=default_tz)
        if s_us > e_us:
            if on_negative == "raise":
                raise NegativeLengthError(
                    f"负长度区间: {start!r} > {end!r}（原始端点不会被自动交换）")
            if on_negative == "drop":
                dropped.append((start, end))
                continue
            if on_negative == "swap":
                swapped.append((start, end))
                s_us, e_us = e_us, s_us
            else:
                raise ValueError(f"未知 on_negative 策略: {on_negative!r}")
        intervals.append(Interval(s_us, e_us))

    before = list(intervals)
    if quantum_us is not None:
        intervals = [quantize(iv, quantum_us) for iv in intervals]

    return MergeReport(merge_sorted(intervals), dropped, swapped, before)


def format_us(us: int) -> str:
    """微秒整数 -> UTC ISO 8601 字符串，便于核对。"""
    return datetime.fromtimestamp(us / US_PER_SECOND,
                                  tz=timezone.utc).isoformat().replace(
        "+00:00", "Z")

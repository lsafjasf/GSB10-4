"""imerge — 时间区间重叠合并库（仅标准库）。

区间语义：半开区间 [start, end)，end 时刻本身不属于区间。
排序规则：先按 start 升序，start 相同按 end 升序（因此同起点的零长度点排在前面）。

合并规则表（规则编号 R1..R8，详见 RULES.md）：
  R1 重叠合并：b.start < cur.end            -> 恒合并（与相接策略无关）
  R2 相接合并：b.start == cur.end           -> 仅 merge_touching=True 时合并
  R3 互不相接：b.start > cur.end            -> 恒不合并
  R4 包含关系：被包含区间并入包含者，结果取 min(start)/max(end)，即 R1 的特例
  R5 零长度点被包含：p 满足 s <= p < e      -> 恒并入区间 [s, e)，点消失
  R6 点恰在区间左端点：p == s               -> 恒并入（[s,e) 包含 s），结果不变
  R7 点恰在区间右端点：p == e               -> 恒不并入（半开区间不含 e），点保留
  R8 负长度区间（start > end）：由 negative_policy 决定
       'strict'    -> 抛 ValueError，不产出任何结果
       'normalize' -> 交换端点翻转；翻转后长度为零则降级为零长度点（走 R5-R7）
  零长度区间（start == end）默认保留为独立点输出，除非被 R5/R6 吸收。

时区与精度统一规则（normalize_datetime_endpoint）：
  T1 所有端点统一为 UTC 时区感知 datetime（aware, tzinfo=utc）。
  T2 无时区信息的输入按 naive_policy 处理：'utc' 视为 UTC（默认），'reject' 报错。
  T3 精度统一截断到微秒：亚微秒部分（如纳秒字符串、float epoch 的浮点尾数）一律
     向下截断（向过去取整），不做四舍五入；被截断的部分记入 diff 报告。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

UTC = timezone.utc
MICROS_PER_SECOND = 1_000_000

Number = Any  # int 或 float；原样保留输入数值类型，不做强制转换（大整数时间轴不丢精度）
Pair = Tuple[Number, Number]

__all__ = [
    "merge_intervals",
    "merge_intervals_reference",
    "normalize_pairs",
    "normalize_datetime_endpoint",
    "normalize_intervals",
    "merge_datetime_intervals",
    "EndpointInfo",
    "IntervalReport",
    "UTC",
]


# ---------------------------------------------------------------------------
# 数值核心：纯 (start, end) 数值对上的合并，供所有上层复用
# ---------------------------------------------------------------------------

def merge_intervals(pairs: Iterable[Pair], merge_touching: bool = False) -> List[Pair]:
    """快速实现：O(n log n) 排序扫描。只接受 start <= end 的区间。

    返回排序后的合并结果列表 [(start, end), ...]，零长度点按 R5-R7 处理。
    """
    items: List[Pair] = [(s, e) for s, e in pairs]
    for s, e in items:
        if e < s:
            raise ValueError(
                "负长度区间 (%r, %r)：请先调用 normalize_pairs()，"
                "或用 negative_policy='normalize'" % (s, e)
            )
    items.sort()
    # items 已按 (start, end) 排序，过滤保持有序：单次排序完成全部工作
    merged: List[List[Number]] = []
    points: List[Number] = []
    last_point: Any = None
    have_point = False
    for s, e in items:
        if e == s:
            if not have_point or s != last_point:  # 相同的点去重后只保留一个
                points.append(s)
                last_point = s
                have_point = True
            continue
        if merged and (s < merged[-1][1] or (merge_touching and s == merged[-1][1])):
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])

    # 零长度点：仅当被某个正长度区间覆盖（s <= p < e）时被吸收，否则保留；
    # points 与 merged 均有序，归并输出，无需再排序
    result: List[Pair] = []
    j = 0
    for p in points:
        while j < len(merged) and merged[j][1] <= p:
            result.append((merged[j][0], merged[j][1]))
            j += 1
        if j < len(merged) and merged[j][0] <= p < merged[j][1]:
            continue  # R5/R6：被包含，吸收
        result.append((p, p))  # R7 或区间外：保留为点
    while j < len(merged):
        result.append((merged[j][0], merged[j][1]))
        j += 1
    return result


def merge_intervals_reference(pairs: Iterable[Pair], merge_touching: bool = False) -> List[Pair]:
    """参考实现：O(n^2) 逐对两两判定 + 并查集求连通分量。

    与快速实现对拍用。两区间按 (start, end) 排序后（a 在前），
    可合并当且仅当：
      b.start < a.end                          （R1 重叠 / R4 包含 / R5 点被包含）
      或 b.start == a.end 且 a 是零长度点       （R6 点在左端点 / 相同点）
      或 b.start == a.end 且 merge_touching 且 b 也是正长度（R2 相接；
         b 是点时为 R7，不合并）
    每个连通分量合并为 (min start, max end)。
    """
    items: List[Pair] = sorted((s, e) for s, e in pairs)
    for s, e in items:
        if e < s:
            raise ValueError("负长度区间 (%r, %r)" % (s, e))
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        s1, e1 = items[i]
        for k in range(i + 1, n):
            s2, e2 = items[k]
            if s2 > e1:
                break  # 已排序，后面只会更远
            if s2 < e1:
                union(i, k)
            elif s2 == e1:
                if e1 == s1:  # a 是零长度点：R6 / 相同点
                    union(i, k)
                elif merge_touching and e2 > s2:  # R2：两个正长度区间相接
                    union(i, k)

    comp: Dict[int, List[Number]] = {}
    for i in range(n):
        root = find(i)
        if root not in comp:
            comp[root] = [items[i][0], items[i][1]]
        else:
            comp[root][0] = min(comp[root][0], items[i][0])
            comp[root][1] = max(comp[root][1], items[i][1])
    return sorted((v[0], v[1]) for v in comp.values())


def normalize_pairs(
    pairs: Iterable[Pair],
    negative_policy: str = "strict",
) -> Tuple[List[Pair], List[Dict[str, Any]]]:
    """按 R8 处理负长度区间，返回 (清洗后的区间列表, 负长度处理记录)。"""
    cleaned: List[Pair] = []
    report: List[Dict[str, Any]] = []
    for idx, (s, e) in enumerate(pairs):
        if e >= s:
            cleaned.append((s, e))
            continue
        if negative_policy == "strict":
            raise ValueError("负长度区间 #%d: (%r, %r)" % (idx, s, e))
        if negative_policy == "normalize":
            flipped = (e, s)
            cleaned.append(flipped)
            report.append({
                "index": idx,
                "input": [s, e],
                "action": "flipped",
                "output": [flipped[0], flipped[1]],
                "note": "交换端点翻转" + ("；翻转后为零长度点" if flipped[0] == flipped[1] else ""),
            })
        else:
            raise ValueError("未知 negative_policy: %r" % negative_policy)
    return cleaned, report


# ---------------------------------------------------------------------------
# 时区 / 精度统一
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2}(?::?\d{2})?)?$"
)


@dataclass
class EndpointInfo:
    """单个端点统一后的信息（统一前 -> 统一后的可核对记录）。"""
    raw: Any
    dt: datetime                    # 统一后：UTC aware，微秒精度
    micros: int                     # dt 距 epoch 的微秒数（整数时间轴）
    tz_assumed: bool = False        # 无时区信息，按 naive_policy 假定为 UTC
    sub_micro: str = ""             # 被截断的亚微秒部分（T3），空串表示无损失


@dataclass
class IntervalReport:
    index: int
    start: EndpointInfo
    end: EndpointInfo
    negative_action: Optional[str] = None  # None / 'flipped'


def _parse_iso(text: str) -> Tuple[datetime, bool, str]:
    m = _ISO_RE.match(text.strip())
    if not m:
        raise ValueError("无法解析的 ISO 时间: %r" % text)
    frac = m.group("frac") or ""
    micro_part = (frac + "000000")[:6]
    sub_micro = frac[6:]  # 亚微秒部分，按 T3 截断
    base = m.group("base")
    tz = m.group("tz") or ""
    iso = base + ("." + micro_part if frac else "") + ("Z" if tz == "Z" else tz)
    dt = datetime.fromisoformat(iso)
    return dt, tz == "", sub_micro


def _datetime_to_micros(dt: datetime) -> int:
    delta = dt - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * MICROS_PER_SECOND + delta.microseconds


def _micros_to_datetime(micros: int) -> datetime:
    whole, frac = divmod(micros, MICROS_PER_SECOND)
    return datetime.fromtimestamp(whole, tz=UTC) + timedelta(microseconds=frac)


def normalize_datetime_endpoint(value: Any, naive_policy: str = "utc") -> EndpointInfo:
    """把单个时间端点统一为 UTC、微秒精度（规则 T1-T3）。

    支持：datetime（aware/naive）、ISO 8601 字符串（可带任意位数小数秒与
    时区偏移）、int/float epoch 秒（一律视为 UTC）。
    """
    tz_assumed = False
    sub_micro = ""
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            if naive_policy == "reject":
                raise ValueError("无时区信息的 datetime: %r" % value)
            dt = dt.replace(tzinfo=UTC)
            tz_assumed = True
    elif isinstance(value, str):
        dt, naive, sub_micro = _parse_iso(value)
        if naive:
            if naive_policy == "reject":
                raise ValueError("无时区信息的 ISO 时间: %r" % value)
            dt = dt.replace(tzinfo=UTC)
            tz_assumed = True
    elif isinstance(value, bool):
        raise TypeError("不支持 bool 作为时间端点")
    elif isinstance(value, int):
        dt = datetime.fromtimestamp(value, tz=UTC)
    elif isinstance(value, float):
        micros_total = int(value * MICROS_PER_SECOND)  # T3：截断，不四舍五入
        whole, frac = divmod(micros_total, MICROS_PER_SECOND)
        dt = datetime.fromtimestamp(whole, tz=UTC) + timedelta(microseconds=frac)
        lost = value * MICROS_PER_SECOND - micros_total
        if lost > 0:
            sub_micro = "float 亚微秒尾数 %.6g us" % (lost,)
    else:
        raise TypeError("不支持的时间端点类型: %r" % type(value))

    dt = dt.astimezone(UTC)  # T1：统一时区到 UTC
    return EndpointInfo(raw=value, dt=dt, micros=_datetime_to_micros(dt),
                        tz_assumed=tz_assumed, sub_micro=sub_micro)


def normalize_intervals(
    intervals: Sequence[Tuple[Any, Any]],
    naive_policy: str = "utc",
    negative_policy: str = "strict",
) -> Tuple[List[Tuple[datetime, datetime]], List[IntervalReport]]:
    """统一整批区间：先按时区/精度规则统一端点，再按 R8 处理负长度。

    返回 (统一后的 [(start_dt, end_dt)], 每个区间的处理报告)。
    negative_policy='strict' 时负长度区间抛 ValueError；'normalize' 时翻转。
    """
    out: List[Tuple[datetime, datetime]] = []
    reports: List[IntervalReport] = []
    for idx, (raw_s, raw_e) in enumerate(intervals):
        s = normalize_datetime_endpoint(raw_s, naive_policy=naive_policy)
        e = normalize_datetime_endpoint(raw_e, naive_policy=naive_policy)
        action = None
        sm, em = s.micros, e.micros
        if em < sm:
            if negative_policy == "strict":
                raise ValueError(
                    "负长度区间 #%d: %r ~ %r（统一后 %s ~ %s）"
                    % (idx, raw_s, raw_e, s.dt.isoformat(), e.dt.isoformat())
                )
            if negative_policy == "normalize":
                sm, em = em, sm
                action = "flipped"
            else:
                raise ValueError("未知 negative_policy: %r" % negative_policy)
        out.append((_micros_to_datetime(sm), _micros_to_datetime(em)))
        reports.append(IntervalReport(index=idx, start=s, end=e, negative_action=action))
    return out, reports


def merge_datetime_intervals(
    intervals: Sequence[Tuple[Any, Any]],
    merge_touching: bool = False,
    naive_policy: str = "utc",
    negative_policy: str = "strict",
) -> Dict[str, Any]:
    """完整流水线：统一时区/精度 -> 处理负长度 -> 合并。

    返回 dict：
      merged   : 合并后的 [(start_dt, end_dt)]（UTC aware，微秒精度）
      merged_iso : 同上，ISO 字符串形式
      reports  : 每个输入区间的统一/翻转报告
      stats    : 输入/输出条数等统计
    """
    normalized, reports = normalize_intervals(
        intervals, naive_policy=naive_policy, negative_policy=negative_policy
    )
    pairs = [(_datetime_to_micros(s), _datetime_to_micros(e)) for s, e in normalized]
    merged_pairs = merge_intervals(pairs, merge_touching=merge_touching)
    merged = [(_micros_to_datetime(s), _micros_to_datetime(e)) for s, e in merged_pairs]
    return {
        "merged": merged,
        "merged_iso": [[s.isoformat(), e.isoformat()] for s, e in merged],
        "reports": reports,
        "stats": {
            "input_count": len(intervals),
            "negative_flipped": sum(1 for r in reports if r.negative_action),
            "output_count": len(merged),
        },
    }

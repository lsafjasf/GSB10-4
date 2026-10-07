"""集合运算的类型统一（type unification for set operations）。

设计要点：
1. 逻辑类型：NULL / BOOL / INT / FLOAT / DECIMAL / STRING / BYTES。
2. 提升规则是一张对称的 join 表（见 PROMOTE_TABLE），NULL 是底元，
   STRING/BYTES 只与自身及 NULL 兼容，其余组合一律拒绝。
3. 统一类型 = 对“全部输入出现过的类型集合”按固定顺序（类型 rank 升序）
   求 join，而不是按行/按集合的先后顺序两两归约，因此结果与运算顺序无关。
4. 不可比类型抛出 TypeUnificationError，携带冲突双方第一次出现的位置。
5. 转换阶段的精度丢失全部记录进 PrecisionReport（计数 + 采样），不静默。
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class LogicalType(IntEnum):
    NULL = 0
    BOOL = 1
    INT = 2
    FLOAT = 3
    DECIMAL = 4
    STRING = 5
    BYTES = 6


# ---------------------------------------------------------------------------
# 提升表（对称，只需存一份）；未列出的非 NULL 组合 = 不可比，拒绝。
# ---------------------------------------------------------------------------
PROMOTE_TABLE: Dict[Tuple[LogicalType, LogicalType], LogicalType] = {
    (LogicalType.BOOL, LogicalType.INT): LogicalType.INT,
    (LogicalType.BOOL, LogicalType.FLOAT): LogicalType.FLOAT,
    (LogicalType.BOOL, LogicalType.DECIMAL): LogicalType.DECIMAL,
    (LogicalType.INT, LogicalType.FLOAT): LogicalType.FLOAT,
    (LogicalType.INT, LogicalType.DECIMAL): LogicalType.DECIMAL,
    (LogicalType.FLOAT, LogicalType.DECIMAL): LogicalType.FLOAT,
}

# float64 能精确表示的最大整数绝对值
_MAX_EXACT_INT_IN_FLOAT = 2 ** 53


class TypeUnificationError(TypeError):
    """不可比类型。携带冲突类型对及各自第一次出现的位置。"""

    def __init__(
        self,
        type_a: LogicalType,
        type_b: LogicalType,
        pos_a: Tuple[int, int],
        pos_b: Tuple[int, int],
    ) -> None:
        self.type_a = type_a
        self.type_b = type_b
        self.pos_a = pos_a  # (collection_index, row_index)
        self.pos_b = pos_b
        super().__init__(
            "incomparable types: %s (first seen at collection %d, row %d) "
            "vs %s (first seen at collection %d, row %d); "
            "refusing to silently coerce to string"
            % (type_a.name, pos_a[0], pos_a[1], type_b.name, pos_b[0], pos_b[1])
        )


@dataclass(frozen=True)
class PrecisionEvent:
    kind: str                      # 'int_to_float' | 'decimal_to_float'
    collection: int
    row: int
    value: str                     # 原值的文本表示
    from_type: str
    to_type: str
    detail: str


class PrecisionReport:
    """精度丢失报告：全量计数 + 有上限的采样，避免百万行时内存爆炸。"""

    def __init__(self, max_samples: int = 100) -> None:
        self.max_samples = max_samples
        self.counts: Counter = Counter()          # (from_type, to_type) -> n
        self.samples: List[PrecisionEvent] = []

    def add(self, event: PrecisionEvent) -> None:
        self.counts[(event.from_type, event.to_type)] += 1
        if len(self.samples) < self.max_samples:
            self.samples.append(event)

    @property
    def has_loss(self) -> bool:
        return bool(self.counts)

    @property
    def total_loss(self) -> int:
        return sum(self.counts.values())

    def merge(self, other: "PrecisionReport") -> None:
        for key, n in other.counts.items():
            self.counts[key] += n
        for ev in other.samples:
            if len(self.samples) >= self.max_samples:
                break
            self.samples.append(ev)

    def summary(self) -> str:
        if not self.counts:
            return "no precision loss"
        parts = ["%s -> %s: %d" % (f, t, n) for (f, t), n in sorted(self.counts.items())]
        return "; ".join(parts)


@dataclass
class SetOpResult:
    values: List[Any]                 # 已转换到统一类型、按规范序排序、去重
    unified_type: LogicalType
    precision: PrecisionReport


# ---------------------------------------------------------------------------
# 类型推断
# ---------------------------------------------------------------------------
def type_of(value: Any) -> LogicalType:
    if value is None:
        return LogicalType.NULL
    if isinstance(value, bool):
        return LogicalType.BOOL
    if isinstance(value, int):
        return LogicalType.INT
    if isinstance(value, float):
        return LogicalType.FLOAT
    if isinstance(value, Decimal):
        return LogicalType.DECIMAL
    if isinstance(value, str):
        return LogicalType.STRING
    if isinstance(value, (bytes, bytearray)):
        return LogicalType.BYTES
    raise TypeError("unsupported value type: %r" % type(value).__name__)


def join_types(a: LogicalType, b: LogicalType) -> Optional[LogicalType]:
    """对称 join；不可比返回 None。NULL 是底元。"""
    if a is b:
        return a
    if a is LogicalType.NULL:
        return b
    if b is LogicalType.NULL:
        return a
    return PROMOTE_TABLE.get((a, b)) or PROMOTE_TABLE.get((b, a))


def unify_types(types: Iterable[LogicalType],
                first_seen: Dict[LogicalType, Tuple[int, int]]) -> LogicalType:
    """对类型集合求统一类型。

    与顺序无关：按 rank 升序扫描，找第一对不可比类型报错（确定性），
    全部可比时在可比子格内归约 join（交换律+结合律成立）。
    """
    present = sorted(set(types))
    if not present:
        return LogicalType.NULL
    for i, ta in enumerate(present):
        for tb in present[i + 1:]:
            if join_types(ta, tb) is None:
                raise TypeUnificationError(ta, tb, first_seen[ta], first_seen[tb])
    result = present[0]
    for t in present[1:]:
        joined = join_types(result, t)
        assert joined is not None
        result = joined
    return result


# ---------------------------------------------------------------------------
# 值转换（精度丢失在此报告）
# ---------------------------------------------------------------------------
def _convert(value: Any, from_t: LogicalType, to_t: LogicalType,
             pos: Tuple[int, int], report: PrecisionReport) -> Any:
    if value is None or from_t is to_t:
        return value
    if to_t is LogicalType.INT and from_t is LogicalType.BOOL:
        return int(value)
    if to_t is LogicalType.DECIMAL and from_t in (LogicalType.BOOL, LogicalType.INT):
        return Decimal(int(value))
    if to_t is LogicalType.FLOAT:
        if from_t is LogicalType.INT:
            if abs(value) > _MAX_EXACT_INT_IN_FLOAT:
                report.add(PrecisionEvent(
                    "int_to_float", pos[0], pos[1], repr(value),
                    "INT", "FLOAT",
                    "|int| > 2**53, not exactly representable as float64"))
            return float(value)
        if from_t is LogicalType.DECIMAL:
            f = float(value)
            if value.is_nan():
                return f
            if Decimal(f) != value:
                report.add(PrecisionEvent(
                    "decimal_to_float", pos[0], pos[1], str(value),
                    "DECIMAL", "FLOAT",
                    "decimal not exactly representable as float64"))
            return f
        return float(value)  # BOOL -> FLOAT
    raise TypeUnificationError(from_t, to_t, pos, pos)  # 理论上不可达


def _canonical_key(value: Any, t: LogicalType) -> Tuple:
    """集合运算用键：同类型内规范化（NaN 归一、-0.0 归一）。"""
    if value is None:
        return (-1,)
    if t is LogicalType.FLOAT:
        if math.isnan(value):
            return (0,)
        return (1, 0.0 if value == 0 else value)
    if t is LogicalType.DECIMAL:
        if value.is_nan():
            return (0,)
        return (1, value)
    return (0, value)


def _sort_key(value: Any, t: LogicalType) -> Tuple:
    """输出排序键：NaN 排最后，保证输出确定。"""
    if value is None:
        return (-1,)
    if t is LogicalType.NULL:
        return (0,)
    if t is LogicalType.FLOAT:
        return (1,) if math.isnan(value) else (0, value)
    if t is LogicalType.DECIMAL:
        return (1,) if value.is_nan() else (0, value)
    return (0, value)


# ---------------------------------------------------------------------------
# 集合运算
# ---------------------------------------------------------------------------
def _prepare(collections: Sequence[Iterable[Any]],
             max_samples: int) -> Tuple[LogicalType, List[List[Any]], PrecisionReport]:
    cols = [list(c) for c in collections]
    first_seen: Dict[LogicalType, Tuple[int, int]] = {}
    for ci, col in enumerate(cols):
        for ri, v in enumerate(col):
            t = type_of(v)
            if t not in first_seen:
                first_seen[t] = (ci, ri)
    unified = unify_types(first_seen.keys(), first_seen)
    report = PrecisionReport(max_samples=max_samples)
    converted: List[List[Any]] = []
    for ci, col in enumerate(cols):
        out = []
        for ri, v in enumerate(col):
            out.append(_convert(v, type_of(v), unified, (ci, ri), report))
        converted.append(out)
    return unified, converted, report


def _require_collections(collections: Tuple) -> None:
    if not collections:
        raise ValueError("at least one collection is required")


def union(*collections: Iterable[Any], max_samples: int = 100) -> SetOpResult:
    _require_collections(collections)
    unified, cols, report = _prepare(collections, max_samples)
    seen: Dict[Tuple, Any] = {}
    for col in cols:
        for v in col:
            seen.setdefault(_canonical_key(v, unified), v)
    values = sorted(seen.values(), key=lambda v: _sort_key(v, unified))
    return SetOpResult(values, unified, report)


def intersection(*collections: Iterable[Any], max_samples: int = 100) -> SetOpResult:
    _require_collections(collections)
    unified, cols, report = _prepare(collections, max_samples)
    keysets = [set(_canonical_key(v, unified) for v in col) for col in cols]
    common = set.intersection(*keysets) if keysets else set()
    seen: Dict[Tuple, Any] = {}
    for v in cols[0]:
        k = _canonical_key(v, unified)
        if k in common:
            seen.setdefault(k, v)
    values = sorted(seen.values(), key=lambda v: _sort_key(v, unified))
    return SetOpResult(values, unified, report)


def difference(first: Iterable[Any], *rest: Iterable[Any],
               max_samples: int = 100) -> SetOpResult:
    unified, cols, report = _prepare((first,) + rest, max_samples)
    excluded = set()
    for col in cols[1:]:
        for v in col:
            excluded.add(_canonical_key(v, unified))
    seen: Dict[Tuple, Any] = {}
    for v in cols[0]:
        k = _canonical_key(v, unified)
        if k not in excluded:
            seen.setdefault(k, v)
    values = sorted(seen.values(), key=lambda v: _sort_key(v, unified))
    return SetOpResult(values, unified, report)

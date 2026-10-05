"""数据集、集合运算（并/交/差）与精度报告。

空值语义：None 作为一个确定的值参与集合运算，None 只等于 None，
同一行里的重复空值按一个值去重。
NaN 语义：float NaN 在集合运算中视为同一个确定值（NaN == NaN）。
-0.0 与 0.0 视为同一个值（Python 哈希/相等语义）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from .errors import (
    IncomparableTypesError,
    SchemaMismatchError,
)
from .types import Type, join
from .values import canonicalize, validate

_UNION = "union"
_INTERSECTION = "intersection"
_DIFFERENCE = "difference"
OPS = (_UNION, _INTERSECTION, _DIFFERENCE)

_NAN_KEY = object()  # NaN 的去重键（哈希一致）


def _value_key(v):
    if isinstance(v, (float, Decimal)) and v != v:  # NaN
        return _NAN_KEY
    return v


def _row_key(row):
    return tuple(_value_key(v) for v in row)


@dataclass
class PrecisionEntry:
    column: int
    src: Type
    dst: Type
    count: int = 0
    examples: list = field(default_factory=list)  # (row, original, converted)


class PrecisionReport:
    """按 (列, 源类型, 目标类型) 聚合的精度丢失报告。"""

    def __init__(self):
        self._entries: dict = {}

    def add(self, row: int, column: int, src: Type, dst: Type, original, converted):
        key = (column, src, dst)
        entry = self._entries.get(key)
        if entry is None:
            entry = PrecisionEntry(column=column, src=src, dst=dst)
            self._entries[key] = entry
        entry.count += 1
        if len(entry.examples) < 3:
            entry.examples.append((row, original, converted))

    @property
    def entries(self) -> list:
        return sorted(self._entries.values(), key=lambda e: (e.column, e.src.name))

    @property
    def total_loss_count(self) -> int:
        return sum(e.count for e in self._entries.values())

    def __bool__(self):
        return bool(self._entries)

    def to_markdown(self) -> str:
        if not self._entries:
            return "无精度丢失。"
        lines = ["| 列 | 源类型 | 目标类型 | 丢失值数 | 示例(行号: 原值 -> 转换值) |",
                 "|---|---|---|---|---|"]
        for e in self.entries:
            ex = "; ".join(
                f"{r}: {o!r} -> {c!r}" for r, o, c in e.examples
            )
            lines.append(
                f"| {e.column} | {e.src.value} | {e.dst.value} | {e.count} | {ex} |"
            )
        return "\n".join(lines)


@dataclass
class Result:
    types: tuple
    rows: list
    precision: PrecisionReport

    def key_set(self):
        return {_row_key(r) for r in self.rows}


class Dataset:
    """带模式的行集合（行在运算前可重复，运算结果去重）。"""

    def __init__(self, types, rows, name: str = "dataset", validate_values: bool = True):
        self.name = name
        self.types = tuple(t if isinstance(t, Type) else Type(t) for t in types)
        self.rows = [tuple(r) for r in rows]
        arity = len(self.types)
        for r in self.rows:
            if len(r) != arity:
                raise ValueError(f"{name}: 行 {r!r} 列数 {len(r)} 与模式 {arity} 不一致")
        if validate_values:
            for ri, row in enumerate(self.rows):
                for ci, (value, t) in enumerate(zip(row, self.types)):
                    validate(value, t, ri, ci)

    def _joined_schema(self, other: "Dataset"):
        if len(self.types) != len(other.types):
            raise SchemaMismatchError(len(self.types), len(other.types))
        joined = []
        for ci, (a, b) in enumerate(zip(self.types, other.types)):
            t = join(a, b)
            if t is None:
                raise IncomparableTypesError(ci, a, b)
            joined.append(t)
        return joined

    def _canonical_rows(self, schema, report: PrecisionReport):
        out = []
        for ri, row in enumerate(self.rows):
            out.append(tuple(
                canonicalize(value, src, dst, report, ri, ci)
                for ci, (value, src, dst) in enumerate(zip(row, self.types, schema))
            ))
        return out

    def _op(self, other: "Dataset", op: str) -> Result:
        schema = tuple(self._joined_schema(other))
        report = PrecisionReport()
        left = self._canonical_rows(schema, report)
        right = other._canonical_rows(schema, report)

        right_keys = {_row_key(r) for r in right}
        if op == _UNION:
            seen = {}
            for r in left:
                seen.setdefault(_row_key(r), r)
            for r in right:
                seen.setdefault(_row_key(r), r)
            rows = list(seen.values())
        elif op == _INTERSECTION:
            seen = {}
            left_keys = set()
            for r in left:
                k = _row_key(r)
                left_keys.add(k)
                seen.setdefault(k, r)
            rows = [seen[k] for k in left_keys if k in right_keys]
        else:  # difference: left - right
            rows = []
            seen = set()
            for r in left:
                k = _row_key(r)
                if k not in right_keys and k not in seen:
                    seen.add(k)
                    rows.append(r)
        return Result(types=schema, rows=rows, precision=report)

    def union(self, other: "Dataset") -> Result:
        return self._op(other, _UNION)

    def intersection(self, other: "Dataset") -> Result:
        return self._op(other, _INTERSECTION)

    def difference(self, other: "Dataset") -> Result:
        return self._op(other, _DIFFERENCE)

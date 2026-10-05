"""类型系统与提升规则（promotion lattice）。

提升规则（join 运算，满足交换律/结合律，因此结果与运算顺序无关）：

1. NULL 与任何类型 T 提升为 T（空值作为值参与运算，不改变类型）。
2. 整数链：bool < int32 < int64 < bigint，取链上较高者。
3. decimal 与整数链提升为 decimal（精确）；decimal 与 float 提升为 float64
   （可能丢精度，需报告）。
4. 浮点：
   - bool + float32          -> float32（0/1 精确）
   - int32 + float32         -> float64（int32 超出 float32 的 24 位尾数，
                                 提升到 float64 避免丢精度）
   - int64/bigint + float*   -> float64（可能丢精度，逐值检测报告）
   - int32/bool + float64    -> float64（精确，int32 < 2^53）
   - float32 + float64       -> float64（widening，精确）
5. date + datetime -> datetime（date 取当日 00:00:00，精确）。
6. 其余组合（string/bytes/数值/日期 跨族）一律不可比，拒绝并报告列位置，
   绝不静默 str() 糊过去。

可能丢精度的组合（逐值检测并计入 PrecisionReport）：
   int64 / bigint / decimal 提升为 float32 / float64 时。
"""
from __future__ import annotations

import enum


class Type(enum.Enum):
    NULL = "null"
    BOOL = "bool"
    INT32 = "int32"
    INT64 = "int64"
    BIGINT = "bigint"
    FLOAT32 = "float32"
    FLOAT64 = "float64"
    DECIMAL = "decimal"
    STRING = "string"
    BYTES = "bytes"
    DATE = "date"
    DATETIME = "datetime"


_INT_CHAIN = (Type.BOOL, Type.INT32, Type.INT64, Type.BIGINT)
_FLOATS = (Type.FLOAT32, Type.FLOAT64)
# 源类型为以下几种、目标为浮点时，需要逐值检查精度
LOSSY_SOURCES = (Type.INT64, Type.BIGINT, Type.DECIMAL)


def join(a: Type, b: Type) -> "Type | None":
    """返回 a、b 的提升结果类型；不可比返回 None。满足交换律与结合律。"""
    if a is b:
        return a
    if a is Type.NULL:
        return b
    if b is Type.NULL:
        return a
    if a in _INT_CHAIN and b in _INT_CHAIN:
        return a if _INT_CHAIN.index(a) > _INT_CHAIN.index(b) else b
    if a is Type.DECIMAL or b is Type.DECIMAL:
        other = b if a is Type.DECIMAL else a
        if other in _INT_CHAIN:
            return Type.DECIMAL
        if other in _FLOATS:
            return Type.FLOAT64
        return None
    if a in _FLOATS and b in _FLOATS:
        return Type.FLOAT64
    if (a in _FLOATS) != (b in _FLOATS):
        # 一个浮点、一个整数链
        f = a if a in _FLOATS else b
        i = b if a in _FLOATS else a
        if i not in _INT_CHAIN:
            return None
        if f is Type.FLOAT32 and i is Type.BOOL:
            return Type.FLOAT32
        return Type.FLOAT64
    if {a, b} == {Type.DATE, Type.DATETIME}:
        return Type.DATETIME
    return None


def is_potentially_lossy(a: Type, b: Type) -> bool:
    """该组合提升后是否可能丢精度（需要逐值检测）。"""
    t = join(a, b)
    if t not in _FLOATS:
        return False
    return a in LOSSY_SOURCES or b in LOSSY_SOURCES

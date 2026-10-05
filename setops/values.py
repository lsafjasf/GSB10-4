"""值校验与提升转换（含逐值精度检测）。"""
from __future__ import annotations

import math
import struct
from datetime import date, datetime, time
from decimal import Decimal

from .errors import ValueConversionError
from .types import Type

_INT32_MIN, _INT32_MAX = -(2 ** 31), 2 ** 31 - 1
_INT64_MIN, _INT64_MAX = -(2 ** 63), 2 ** 63 - 1


def validate(value, t: Type, row: int, column: int) -> None:
    """校验 Python 值与其声明类型一致；不一致抛 ValueConversionError（带行/列位置）。"""
    if value is None:
        return  # 任何列都允许空值（nullable）
    if t is Type.NULL:
        if type(value) is not type(None):
            raise ValueConversionError(row, column, value, t)
    elif t is Type.BOOL:
        if type(value) is not bool:
            raise ValueConversionError(row, column, value, t)
    elif t in (Type.INT32, Type.INT64, Type.BIGINT):
        if type(value) is not int:  # 排除 bool（bool 是 int 子类）
            raise ValueConversionError(row, column, value, t)
        if t is Type.INT32 and not (_INT32_MIN <= value <= _INT32_MAX):
            raise ValueConversionError(row, column, value, t)
        if t is Type.INT64 and not (_INT64_MIN <= value <= _INT64_MAX):
            raise ValueConversionError(row, column, value, t)
    elif t in (Type.FLOAT32, Type.FLOAT64):
        if type(value) is not float:
            raise ValueConversionError(row, column, value, t)
    elif t is Type.DECIMAL:
        if not isinstance(value, Decimal):
            raise ValueConversionError(row, column, value, t)
    elif t is Type.STRING:
        if type(value) is not str:
            raise ValueConversionError(row, column, value, t)
    elif t is Type.BYTES:
        if type(value) is not bytes:
            raise ValueConversionError(row, column, value, t)
    elif t is Type.DATE:
        if type(value) is not date:  # 排除 datetime 子类
            raise ValueConversionError(row, column, value, t)
    elif t is Type.DATETIME:
        if type(value) is not datetime:
            raise ValueConversionError(row, column, value, t)


def _to_float32(value) -> float:
    try:
        return struct.unpack("<f", struct.pack("<f", float(value)))[0]
    except OverflowError:
        return math.copysign(math.inf, value) if value else 0.0


def canonicalize(value, src: Type, dst: Type, report, row: int, column: int):
    """把 src 类型的值转换为提升目标 dst 类型的规范化表示。

    空值保持 None。发现精度丢失时记录到 report。
    """
    if value is None:
        return None
    if src is dst:
        return value

    # 整数链内部（含 bool -> int）
    if dst in (Type.INT32, Type.INT64, Type.BIGINT):
        return int(value)

    # 目标为 decimal：整数/bool 精确
    if dst is Type.DECIMAL:
        return Decimal(value)

    # date -> datetime（精确，取 00:00:00）
    if dst is Type.DATETIME and src is Type.DATE:
        return datetime.combine(value, time.min)

    # 目标为浮点
    if dst in (Type.FLOAT32, Type.FLOAT64):
        converted = _to_float_with_precision_check(value, src, report, row, column)
        if dst is Type.FLOAT32:
            return _to_float32(converted)
        return converted

    raise ValueConversionError(row, column, value, dst)


def _to_float_with_precision_check(value, src: Type, report, row: int, column: int):
    if src in (Type.BOOL, Type.INT32, Type.FLOAT32, Type.FLOAT64):
        return float(value)
    if src in (Type.INT64, Type.BIGINT):
        try:
            f = float(value)
        except OverflowError:
            f = math.inf if value >= 0 else -math.inf
            report.add(row, column, src, Type.FLOAT64, value, f)
            return f
        # 超出 2^53 的整数可能无法被 float64 精确表示
        if math.isinf(f) or (f.is_integer() and int(f) != value):
            report.add(row, column, src, Type.FLOAT64, value, f)
        return f
    if src is Type.DECIMAL:
        f = float(value)
        if math.isinf(f) or math.isnan(f) or Decimal.from_float(f) != value:
            report.add(row, column, src, Type.FLOAT64, value, f)
        return f
    return float(value)

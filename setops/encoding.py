"""值的 JSON 可序列化编码/解码（用于对拍数据文件，仅标准库）。"""
from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal


def encode_value(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return {"b": v}
    if isinstance(v, int):
        return {"i": str(v)}  # bigint 安全
    if isinstance(v, float):
        if math.isnan(v):
            return {"f": "nan"}
        if math.isinf(v):
            return {"f": "inf" if v > 0 else "-inf"}
        return {"f": v.hex()}  # 精确
    if isinstance(v, Decimal):
        return {"d": str(v)}
    if isinstance(v, str):
        return {"s": v}
    if isinstance(v, bytes):
        return {"x": v.hex()}
    if isinstance(v, datetime):
        return {"t": v.isoformat()}
    if isinstance(v, date):
        return {"D": v.isoformat()}
    raise TypeError(f"无法编码的值: {v!r}")


def decode_value(v):
    if v is None:
        return None
    (tag, raw), = v.items()
    if tag == "b":
        return bool(raw)
    if tag == "i":
        return int(raw)
    if tag == "f":
        if raw == "nan":
            return float("nan")
        if raw == "inf":
            return float("inf")
        if raw == "-inf":
            return float("-inf")
        return float.fromhex(raw)
    if tag == "d":
        return Decimal(raw)
    if tag == "s":
        return raw
    if tag == "x":
        return bytes.fromhex(raw)
    if tag == "t":
        return datetime.fromisoformat(raw)
    if tag == "D":
        return date.fromisoformat(raw)
    raise ValueError(f"未知编码: {v!r}")


def encode_row(row):
    return [encode_value(v) for v in row]


def decode_row(row):
    return tuple(decode_value(v) for v in row)

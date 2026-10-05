"""各类型样本值生成（供对拍数据生成器与基准测试共用）。"""
from __future__ import annotations

import random
import struct
from datetime import date, datetime, timedelta
from decimal import Decimal

from setops import Type


def _f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


_WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]


def sample_value(t: Type, rng: random.Random):
    """按类型生成样本值；数值池故意做小，保证集合运算有交集/重复。"""
    if t is Type.NULL:
        return None
    if t is Type.BOOL:
        return rng.random() < 0.5
    if t is Type.INT32:
        return rng.randint(-50, 50)
    if t is Type.INT64:
        # 混入 > 2^53 的值以触发精度检测
        if rng.random() < 0.1:
            return 2 ** 53 + rng.randint(1, 1000)
        return rng.randint(-50, 50)
    if t is Type.BIGINT:
        if rng.random() < 0.2:
            return 2 ** 70 + rng.randint(0, 1000)
        return rng.randint(-50, 50)
    if t is Type.FLOAT32:
        return _f32(rng.choice([0.5, 1.5, -2.25, 3.0, 0.1, 100.0]))
    if t is Type.FLOAT64:
        return rng.choice([0.5, 1.5, -2.25, 3.0, 0.1, 100.0, 1e300])
    if t is Type.DECIMAL:
        return Decimal(rng.choice(["0.1", "1.5", "-2.25", "3", "0.125", "100"]))
    if t is Type.STRING:
        return rng.choice(_WORDS)
    if t is Type.BYTES:
        return rng.choice(_WORDS).encode()
    if t is Type.DATE:
        return date(2024, 1, 1) + timedelta(days=rng.randint(0, 30))
    if t is Type.DATETIME:
        return datetime(2024, 1, 1) + timedelta(
            days=rng.randint(0, 30), hours=rng.randint(0, 23)
        )
    raise ValueError(t)


def sample_row(types, rng: random.Random, null_rate: float = 0.0):
    row = []
    for t in types:
        if null_rate and rng.random() < null_rate:
            row.append(None)
        else:
            row.append(sample_value(t, rng))
    return tuple(row)

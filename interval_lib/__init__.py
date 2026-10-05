"""区间算术与误差传播库（仅依赖 Python 3 标准库）。

导出：
    Interval              闭/开端点区间（机器数，外向舍入）
    IntervalDivisionByZero 除数含 0 时抛出，绝不返回“看似正常”的区间
    IntervalEmptyError    在无定义点上求函数（如 log 负区间）时抛出
    IntervalUnion         扩展除法的结果：若干区间/射线之并
    div_extended          扩展除法，除零显式返回含无穷的并集（非普通区间）
    Affine                仿射形式（一阶误差模型），用于缓解宽度膨胀
"""

from .interval import (
    Interval,
    IntervalDivisionByZero,
    IntervalEmptyError,
    IntervalUnion,
    Ray,
    div_extended,
)
from .affine import Affine

__all__ = [
    "Interval",
    "IntervalDivisionByZero",
    "IntervalEmptyError",
    "IntervalUnion",
    "Ray",
    "div_extended",
    "Affine",
]

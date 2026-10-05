"""错误类型：不可比类型拒绝、值级转换错误，均带位置信息。"""
from __future__ import annotations

from .types import Type


class IncomparableTypesError(TypeError):
    """两列类型不可比/不可提升。column 为 0 起始的列位置。"""

    def __init__(self, column: int, left: Type, right: Type):
        self.column = column
        self.left = left
        self.right = right
        super().__init__(
            f"列 {column}: 类型不可比，拒绝运算: "
            f"{left.value} vs {right.value}（不做静默字符串转换）"
        )


class SchemaMismatchError(ValueError):
    def __init__(self, left_arity: int, right_arity: int):
        super().__init__(
            f"列数不一致: 左 {left_arity} 列 vs 右 {right_arity} 列"
        )


class ValueConversionError(ValueError):
    """值与声明类型不符。row/column 为 0 起始位置。"""

    def __init__(self, row: int, column: int, value, expected: Type):
        self.row = row
        self.column = column
        self.value = value
        self.expected = expected
        super().__init__(
            f"行 {row} 列 {column}: 值 {value!r} 不符合声明类型 {expected.value}"
        )

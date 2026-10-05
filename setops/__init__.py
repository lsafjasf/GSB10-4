"""setops: 带类型提升（type unification）的集合运算库。仅依赖标准库。"""
from .dataset import Dataset, PrecisionReport, Result
from .errors import (
    IncomparableTypesError,
    SchemaMismatchError,
    ValueConversionError,
)
from .types import Type, join, is_potentially_lossy

__all__ = [
    "Dataset",
    "PrecisionReport",
    "Result",
    "Type",
    "join",
    "is_potentially_lossy",
    "IncomparableTypesError",
    "SchemaMismatchError",
    "ValueConversionError",
]

"""稳定的多级排序库（仅标准库）。

对外导出：
    SortKey   单级排序键的定义
    stable_sort  一次成型的稳定多级排序
    multipass_sort  逐级稳定排序（对拍用，结果应与 stable_sort 完全一致）
    explain_vs_primary  对比"只按主键排序"的差异
"""

from .stable import (
    SortKey,
    stable_sort,
    multipass_sort,
    explain_vs_primary,
    MISSING,
)

__all__ = [
    "SortKey",
    "stable_sort",
    "multipass_sort",
    "explain_vs_primary",
    "MISSING",
]

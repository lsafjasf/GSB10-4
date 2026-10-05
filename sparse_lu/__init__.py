"""sparse_lu: 稀疏矩阵分块 LU 分解（仅标准库）。

公共 API:
  SparseMatrix / 矩阵生成器          -> sparse_lu.matrix
  natural_order / rcm_order / min_degree_order
  symbolic_lu                        -> 符号分解（只数填充元）
  factor                             -> 数值分块 LU（带内存守卫与溢写）
  iterative_refinement               -> 迭代精化
  MemoryBudgetExceeded / ZeroPivotError
"""

from .matrix import (
    SparseMatrix,
    diagonal_matrix,
    banded_matrix,
    random_sparse_matrix,
    near_dense_matrix,
    poisson_2d_matrix,
    ill_conditioned_banded,
)
from .reorder import natural_order, rcm_order, min_degree_order
from .symbolic import symbolic_lu, SymbolicResult
from .memory import MemoryBudgetExceeded, MemoryGuard, current_rss_bytes, max_rss_bytes
from .factor import factor, factor_residual, LUFactors, ZeroPivotError
from .refine import iterative_refinement

__all__ = [
    "SparseMatrix",
    "diagonal_matrix",
    "banded_matrix",
    "random_sparse_matrix",
    "near_dense_matrix",
    "poisson_2d_matrix",
    "ill_conditioned_banded",
    "natural_order",
    "rcm_order",
    "min_degree_order",
    "symbolic_lu",
    "SymbolicResult",
    "MemoryBudgetExceeded",
    "MemoryGuard",
    "current_rss_bytes",
    "max_rss_bytes",
    "factor",
    "factor_residual",
    "LUFactors",
    "ZeroPivotError",
    "iterative_refinement",
]

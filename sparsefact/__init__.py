"""sparsefact: blocked sparse Cholesky factorization, pure stdlib.

Public API:
    SparseMatrix, generators (diagonal, banded, random_sparse, near_dense,
    grid9), permuted, random_permutation
    orderings: identity, rcm, minimum_degree
    symbolic_cholesky, estimate_flops
    factorize, Factor
    residual_norm, iterative_refinement
    MemoryBudgetExceeded
"""

from .errors import MemoryBudgetExceeded
from .matrix import (SparseMatrix, diagonal, banded, random_sparse,
                     near_dense, grid9, permuted, random_permutation)
from .ordering import identity, rcm, minimum_degree, ORDERINGS
from .symbolic import symbolic_cholesky, estimate_flops
from .factor import factorize, Factor
from .refine import residual_norm, iterative_refinement

__all__ = [
    "MemoryBudgetExceeded", "SparseMatrix", "diagonal", "banded",
    "random_sparse", "near_dense", "grid9", "permuted",
    "random_permutation", "identity", "rcm", "minimum_degree", "ORDERINGS",
    "symbolic_cholesky", "estimate_flops", "factorize", "Factor",
    "residual_norm", "iterative_refinement",
]

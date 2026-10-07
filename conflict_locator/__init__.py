"""约束冲突定位库：差分约束的最小冲突集合（MUS）提取、枚举与验证。"""

from .constraints import Constraint, DifferenceSystem, CheckResult, normalize_ids
from .mus import (MinimalConflict, find_one_mus, enumerate_mus, find_some_mus,
                  verify_mus, extract_mus_deletion, brute_force_mus)
from .report import Diagnosis, diagnose, render_report

__all__ = [
    "Constraint", "DifferenceSystem", "CheckResult", "normalize_ids",
    "MinimalConflict", "find_one_mus", "enumerate_mus", "find_some_mus",
    "verify_mus", "extract_mus_deletion", "brute_force_mus",
    "Diagnosis", "diagnose", "render_report",
]

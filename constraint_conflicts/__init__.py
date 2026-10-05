"""Minimal-conflict reporting for difference constraints."""

from .difference_logic import DifferenceConstraint, DifferenceSystem
from .report import AnalysisReport, ConflictSet, analyze

__all__ = [
    "AnalysisReport",
    "ConflictSet",
    "DifferenceConstraint",
    "DifferenceSystem",
    "analyze",
]

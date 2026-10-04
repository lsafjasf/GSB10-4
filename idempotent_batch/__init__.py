"""幂等批量提交 + 逐项重试（仅标准库）。"""
from .errors import ErrorKind, BatchError, CATALOG, classify
from .server import FakeServer, Workload
from .client import Item, ItemStatus, submit_batch, submit_single, Stats

__all__ = [
    "ErrorKind", "BatchError", "CATALOG", "classify",
    "FakeServer", "Workload",
    "Item", "ItemStatus", "submit_batch", "submit_single", "Stats",
]

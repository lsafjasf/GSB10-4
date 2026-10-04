"""logshard: 按大小轮转的字节级日志分片与流式合并（仅标准库）。"""

from .format import FormatError, format_record, parse_record
from .reader import (
    DanglingHeadEvent,
    DanglingTailEvent,
    DuplicateSeqEvent,
    GapEvent,
    MergeReport,
    MergedRecord,
    Merger,
    ParseErrorEvent,
    RollbackEvent,
    ShardMeta,
    TruncatedTailEvent,
    discover_shards,
)
from .writer import RotatingWriter, SeqLogger

__version__ = "1.0.0"

__all__ = [
    "FormatError",
    "format_record",
    "parse_record",
    "RotatingWriter",
    "SeqLogger",
    "Merger",
    "MergeReport",
    "MergedRecord",
    "ShardMeta",
    "GapEvent",
    "DanglingHeadEvent",
    "DanglingTailEvent",
    "TruncatedTailEvent",
    "RollbackEvent",
    "DuplicateSeqEvent",
    "ParseErrorEvent",
    "discover_shards",
]

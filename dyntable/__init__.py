"""Dynamic header-compression table with synchronized encoder/decoder."""

from .codec import Decoder, Encoder
from .errors import (
    BlockedHeaderBlock,
    CapacityLimitExceeded,
    DynamicTableError,
    EncoderStreamError,
    EvictedEntryReference,
    IndexOutOfRange,
    InvalidHeaderBlock,
    InvalidInstruction,
    LostUpdateError,
    OutOfOrderUpdateError,
    TableInvariantViolation,
    TruncatedData,
)
from .table import ENTRY_OVERHEAD, DynamicTable, entry_size

__all__ = [
    "BlockedHeaderBlock",
    "CapacityLimitExceeded",
    "Decoder",
    "DynamicTable",
    "DynamicTableError",
    "ENTRY_OVERHEAD",
    "Encoder",
    "EncoderStreamError",
    "EvictedEntryReference",
    "IndexOutOfRange",
    "InvalidHeaderBlock",
    "InvalidInstruction",
    "LostUpdateError",
    "OutOfOrderUpdateError",
    "TableInvariantViolation",
    "TruncatedData",
    "entry_size",
]

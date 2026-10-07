"""Error types for the dynamic-table package.

Every abnormal condition is reported by raising one of these errors;
the decoder never silently produces wrong headers.
"""


class DynamicTableError(Exception):
    """Base class for all dynamic-table related errors."""


class CapacityLimitExceeded(DynamicTableError):
    """A dynamic-table capacity update exceeds the protocol upper bound."""


class TableInvariantViolation(DynamicTableError):
    """Internal size accounting invariant is broken (table overflow)."""


class IndexOutOfRange(DynamicTableError):
    """An index refers to an entry that was never inserted."""


class EvictedEntryReference(IndexOutOfRange):
    """An index refers to an entry that has already been evicted."""


class TruncatedData(DynamicTableError):
    """A wire buffer ended before a complete item could be decoded."""


class EncoderStreamError(DynamicTableError):
    """Base class for encoder-instruction-stream errors."""


class LostUpdateError(EncoderStreamError):
    """A gap in the stream sequence: one or more update chunks were lost."""


class OutOfOrderUpdateError(EncoderStreamError):
    """A chunk arrived with a sequence number lower than expected."""


class InvalidInstruction(EncoderStreamError):
    """An update instruction cannot be executed (bad encoding/violation)."""


class BlockedHeaderBlock(DynamicTableError):
    """The header block references table entries not yet received."""


class InvalidHeaderBlock(DynamicTableError):
    """The header block prefix or fields are malformed."""

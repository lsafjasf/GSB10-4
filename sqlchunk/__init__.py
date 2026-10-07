"""sqlchunk: statement-boundary-safe SQL script chunking and resumable execution.

Only Python 3 standard library is used.
"""

from .lexer import split_statements, Statement
from .chunker import build_chunks, Chunk, split_and_chunk
from .checkpoint import (
    CheckpointStore,
    CheckpointCorrupted,
    ChunkRecord,
    STATUS_PENDING,
    STATUS_DONE,
    STATUS_FAILED_RETRYABLE,
    STATUS_FAILED_FATAL,
)
from .executor import (
    ChunkExecutor,
    RetryPolicy,
    ChunkResult,
    ExecutionSummary,
    RetryableError,
)

__all__ = [
    "split_statements",
    "Statement",
    "build_chunks",
    "Chunk",
    "split_and_chunk",
    "CheckpointStore",
    "CheckpointCorrupted",
    "ChunkRecord",
    "STATUS_PENDING",
    "STATUS_DONE",
    "STATUS_FAILED_RETRYABLE",
    "STATUS_FAILED_FATAL",
    "ChunkExecutor",
    "RetryPolicy",
    "ChunkResult",
    "ExecutionSummary",
    "RetryableError",
]

VERSION = "1.0.0"

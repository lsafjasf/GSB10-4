"""Group complete statements into size-bounded chunks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .lexer import Statement, split_statements

__all__ = ["Chunk", "build_chunks", "split_and_chunk"]


@dataclass(frozen=True)
class Chunk:
    index: int
    start_stmt: int  # inclusive statement index
    end_stmt: int  # exclusive statement index
    byte_start: int  # offset of first statement in the source script
    byte_end: int  # offset after last statement
    text: bytes  # exact text to execute (concatenation of statement texts)
    sha256: str
    statements: tuple[Statement, ...]
    oversized: bool = False  # single statement larger than max_chunk_bytes

    @property
    def size(self) -> int:
        return len(self.text)

    @property
    def statement_count(self) -> int:
        return self.end_stmt - self.start_stmt


def _chunk_text(stmts: list[Statement]) -> bytes:
    return b"".join(s.text for s in stmts)


def build_chunks(
    statements: list[Statement], max_chunk_bytes: int = 1 << 20
) -> list[Chunk]:
    """Greedy packing of statements into chunks of at most *max_chunk_bytes*.

    A statement larger than the limit becomes its own chunk flagged
    ``oversized``: statement integrity always takes precedence over the
    size limit.
    """

    if max_chunk_bytes <= 0:
        raise ValueError("max_chunk_bytes must be positive")

    chunks: list[Chunk] = []
    group: list[Statement] = []
    group_size = 0
    group_start = 0

    def flush() -> None:
        nonlocal group, group_size, group_start
        if not group:
            return
        text = _chunk_text(group)
        chunks.append(
            Chunk(
                index=len(chunks),
                start_stmt=group[0].index,
                end_stmt=group[-1].index + 1,
                byte_start=group[0].start,
                byte_end=group[-1].end,
                text=text,
                sha256=hashlib.sha256(text).hexdigest(),
                statements=tuple(group),
                oversized=len(group) == 1 and len(text) > max_chunk_bytes,
            )
        )
        group = []
        group_size = 0

    for stmt in statements:
        size = len(stmt.text)
        if group and group_size + size > max_chunk_bytes:
            flush()
        if not group:
            group_start = stmt.index
        group.append(stmt)
        group_size += size

    flush()
    return chunks


def split_and_chunk(
    script: bytes, max_chunk_bytes: int = 1 << 20
) -> list[Chunk]:
    return build_chunks(split_statements(script), max_chunk_bytes)

"""把语句序列打包成块（chunk），块边界永远落在语句边界上。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .splitter import split_statements


@dataclass
class Chunk:
    index: int
    statements: List[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return ";\n".join(self.statements) + ";\n"

    @property
    def stmt_count(self) -> int:
        return len(self.statements)

    @property
    def byte_size(self) -> int:
        return len(self.text.encode("utf-8"))


def build_chunks(script: str, max_chunk_bytes: int = 1 << 20) -> List[Chunk]:
    """按字节预算把语句装块。

    规则：
    - 逐语句累加，加入下一条会超预算则封块（块内至少 1 条）。
    - 单条语句自身超过预算时，独占一块（超大块），不拆分语句。
    """
    statements = split_statements(script)
    chunks: List[Chunk] = []
    current: List[str] = []
    current_bytes = 0

    def flush():
        nonlocal current, current_bytes
        if current:
            chunks.append(Chunk(index=len(chunks), statements=current))
            current = []
            current_bytes = 0

    for _, stmt in statements:
        stmt_bytes = len(stmt.encode("utf-8")) + 2  # ';\n'
        if current and current_bytes + stmt_bytes > max_chunk_bytes:
            flush()
        current.append(stmt)
        current_bytes += stmt_bytes
        if stmt_bytes > max_chunk_bytes:
            flush()  # 超大语句独占一块
    flush()
    return chunks

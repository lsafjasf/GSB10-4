"""按字节大小轮转的分片写入器。

轮转是纯字节行为：分片大小达到阈值时立即关闭旧文件、开新文件，
一条记录完全可能被切成前后两半（前半留在旧分片尾部，后半进入新分片头部之后）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

from .format import format_marker, format_record


class RotatingWriter:
    """把字节流切成 <= max_bytes 的分片文件。

    参数:
        out_dir: 分片输出目录
        max_bytes: 单个分片的字节上限（分片头也计入占用）
        prefix: 分片文件名前缀，最终名为 ``prefix-000001`` 之类（文件名不承载顺序）

    新分片总是先写头 ``#LSHRD v1 offset=<全局字节偏移>``。
    """

    def __init__(self, out_dir: str | os.PathLike[str], max_bytes: int, prefix: str = "log.shard") -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.prefix = prefix
        self._index = 0
        self._stream_offset = 0  # 下一个要写字节在全局流中的绝对偏移
        self._fh: BinaryIO | None = None
        self._used = 0  # 当前分片已写字节数（含头）

    @property
    def stream_offset(self) -> int:
        return self._stream_offset

    @property
    def shard_count(self) -> int:
        return self._index

    def _open_new_shard(self) -> None:
        if self._fh is not None:
            self._fh.close()
        self._index += 1
        path = self.out_dir / f"{self.prefix}-{self._index:06d}"
        self._fh = open(path, "wb")
        head = format_marker(self._stream_offset)
        if len(head) >= self.max_bytes:
            raise ValueError(
                f"max_bytes={self.max_bytes} too small for shard header ({len(head)} bytes) at offset {self._stream_offset}"
            )
        self._fh.write(head)
        self._used = len(head)

    def _write_blob(self, blob: bytes) -> None:
        """把任意 blob 按 max_bytes 切开，必要时在 blob 中间开新分片。"""
        pos = 0
        while pos < len(blob):
            if self._fh is None:
                self._open_new_shard()
            assert self._fh is not None
            room = self.max_bytes - self._used
            if room <= 0:
                self._open_new_shard()  # 头装不下时会抛 ValueError，不会死循环
                continue
            chunk = blob[pos : pos + room]
            self._fh.write(chunk)
            self._used += len(chunk)
            self._stream_offset += len(chunk)
            pos += len(chunk)
            if self._used >= self.max_bytes and pos < len(blob):
                self._open_new_shard()

    def write_line_bytes(self, line: bytes) -> None:
        """写入一行原始字节（必须自带 ``\\n``），可能跨分片。"""
        if not line.endswith(b"\n"):
            raise ValueError("line must end with newline")
        self._write_blob(line)

    def write_record(self, ts_ms: int, seq: int, payload: bytes) -> None:
        self.write_line_bytes(format_record(ts_ms, seq, payload))

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "RotatingWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class SeqLogger:
    """在 :class:`RotatingWriter` 之上提供自动序号的便捷封装。"""

    def __init__(self, writer: RotatingWriter, start_seq: int = 1) -> None:
        self.writer = writer
        self._seq = start_seq

    @property
    def next_seq(self) -> int:
        return self._seq

    def log(self, ts_ms: int, payload: bytes) -> int:
        seq = self._seq
        self.writer.write_record(ts_ms, seq, payload)
        self._seq += 1
        return seq

    def close(self) -> None:
        self.writer.close()

    def __enter__(self) -> "SeqLogger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

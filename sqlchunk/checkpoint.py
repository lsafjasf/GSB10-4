"""Durable checkpoint storage (JSON, atomically replaced)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

__all__ = [
    "CheckpointCorrupted",
    "ChunkRecord",
    "CheckpointStore",
    "STATUS_PENDING",
    "STATUS_DONE",
    "STATUS_FAILED_RETRYABLE",
    "STATUS_FAILED_FATAL",
]

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED_RETRYABLE = "failed_retryable"
STATUS_FAILED_FATAL = "failed_fatal"

FORMAT_VERSION = 1
_REQUIRED_TOP = {"version", "script_sha256", "script_bytes", "chunks"}
_REQUIRED_CHUNK = {
    "index",
    "start_stmt",
    "end_stmt",
    "byte_start",
    "byte_end",
    "sha256",
    "statements",
    "bytes",
    "oversized",
    "status",
    "attempts",
}


class CheckpointCorrupted(Exception):
    """Checkpoint file cannot be parsed or does not match the script."""


@dataclass
class ChunkRecord:
    index: int
    start_stmt: int
    end_stmt: int
    byte_start: int
    byte_end: int
    sha256: str
    statements: int
    bytes: int
    oversized: bool
    status: str = STATUS_PENDING
    attempts: int = 0
    retries: int = 0
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChunkRecord":
        missing = _REQUIRED_CHUNK - d.keys()
        if missing:
            raise CheckpointCorrupted(
                f"chunk record missing keys: {sorted(missing)}"
            )
        return cls(**{k: d[k] for k in cls.__dataclass_fields__})


def _atomic_write(path: str, payload: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".ckpt.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class CheckpointStore:
    """Owns the on-disk checkpoint and proves execution progress.

    Progress proof consists of:
      * SHA-256 + byte length of the whole source script;
      * per-chunk SHA-256 of the exact executed bytes;
      * per-chunk status / attempt counters / error info / timestamps.
    """

    def __init__(
        self,
        path: str,
        script: bytes,
        chunks,
        chunk_size: Optional[int] = None,
    ):
        self.path = path
        self.script = script
        self.chunks = list(chunks)
        self.script_sha256 = hashlib.sha256(script).hexdigest()
        self.script_bytes = len(script)
        self.chunk_size = chunk_size
        self.records: list[ChunkRecord] = []
        self.created_at: Optional[float] = None
        self.updated_at: Optional[float] = None

    # ------------------------------------------------------------------ create
    def init_new(self) -> None:
        now = time.time()
        self.created_at = now
        self.updated_at = now
        self.records = [
            ChunkRecord(
                index=c.index,
                start_stmt=c.start_stmt,
                end_stmt=c.end_stmt,
                byte_start=c.byte_start,
                byte_end=c.byte_end,
                sha256=c.sha256,
                statements=c.statement_count,
                bytes=c.size,
                oversized=c.oversized,
            )
            for c in self.chunks
        ]
        self.save()

    # -------------------------------------------------------------- serialize
    def to_dict(self) -> dict:
        return {
            "version": FORMAT_VERSION,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "script_sha256": self.script_sha256,
            "script_bytes": self.script_bytes,
            "chunk_size": self.chunk_size,
            "statement_total": sum(c.statement_count for c in self.chunks),
            "chunk_total": len(self.chunks),
            "chunks": [r.to_dict() for r in self.records],
        }

    @classmethod
    def load(cls, path: str, script: bytes, chunks) -> "CheckpointStore":
        """Load and fully validate a checkpoint for this exact script."""
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except FileNotFoundError:
            raise
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CheckpointCorrupted(f"checkpoint is not valid JSON: {exc}")
        if not isinstance(data, dict):
            raise CheckpointCorrupted("checkpoint root is not an object")
        missing = _REQUIRED_TOP - data.keys()
        if missing:
            raise CheckpointCorrupted(
                f"checkpoint missing keys: {sorted(missing)}"
            )
        if data["version"] != FORMAT_VERSION:
            raise CheckpointCorrupted(
                f"unsupported checkpoint version: {data['version']}"
            )

        store = cls(path, script, chunks, data.get("chunk_size"))
        if data["script_sha256"] != store.script_sha256:
            raise CheckpointCorrupted(
                "checkpoint was written for a different script "
                "(script_sha256 mismatch); refusing to resume to avoid "
                "replaying side effects"
            )
        if data["script_bytes"] != store.script_bytes:
            raise CheckpointCorrupted(
                "checkpoint script length does not match the current script"
            )
        raw_records = data["chunks"]
        if not isinstance(raw_records, list):
            raise CheckpointCorrupted("chunks is not a list")
        if len(raw_records) != len(store.chunks):
            raise CheckpointCorrupted(
                f"chunk count mismatch: checkpoint={len(raw_records)} "
                f"current={len(store.chunks)}"
            )
        records = []
        for pos, item in enumerate(raw_records):
            if not isinstance(item, dict):
                raise CheckpointCorrupted(f"chunk #{pos} is not an object")
            rec = ChunkRecord.from_dict(item)
            chunk = store.chunks[pos]
            if rec.index != chunk.index:
                raise CheckpointCorrupted(f"chunk #{pos} index mismatch")
            if rec.sha256 != chunk.sha256:
                raise CheckpointCorrupted(
                    f"chunk #{pos} sha256 mismatch; chunk content differs "
                    "from the checkpointed script"
                )
            if (
                rec.start_stmt != chunk.start_stmt
                or rec.end_stmt != chunk.end_stmt
                or rec.bytes != chunk.size
            ):
                raise CheckpointCorrupted(f"chunk #{pos} metadata mismatch")
            records.append(rec)
        store.records = records
        store.created_at = data.get("created_at")
        store.updated_at = data.get("updated_at")
        return store

    @classmethod
    def open_or_create(cls, path: str, script: bytes, chunks,
                       chunk_size=None) -> "CheckpointStore":
        store = cls(path, script, chunks, chunk_size)
        try:
            loaded = cls.load(path, script, chunks)
        except FileNotFoundError:
            store.init_new()
            return store
        return loaded

    # ----------------------------------------------------------------- mutate
    def mark_started(self, index: int) -> None:
        rec = self.records[index]
        rec.attempts += 1
        rec.started_at = time.time()
        self.save()

    def mark_done(self, index: int) -> None:
        rec = self.records[index]
        rec.status = STATUS_DONE
        rec.retries = max(0, rec.attempts - 1)
        rec.error_type = None
        rec.error_message = None
        rec.finished_at = time.time()
        self.updated_at = time.time()
        self.save()

    def mark_failed(self, index: int, exc: BaseException, retryable: bool) -> None:
        rec = self.records[index]
        rec.status = STATUS_FAILED_RETRYABLE if retryable else STATUS_FAILED_FATAL
        rec.retries = max(0, rec.attempts - 1)
        rec.error_type = type(exc).__name__
        rec.error_message = str(exc)[:1000]
        rec.finished_at = time.time()
        self.updated_at = time.time()
        self.save()

    def next_pending(self) -> Optional[int]:
        """First chunk index that is not proven done.

        A chunk previously marked failed_retryable is still resumable; a
        failed_fatal chunk blocks progress and is returned so the caller can
        surface the recorded error.
        """
        for rec in self.records:
            if rec.status != STATUS_DONE:
                return rec.index
        return None

    def save(self) -> None:
        payload = (
            json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        _atomic_write(self.path, payload)

    # ------------------------------------------------------------------ stats
    def done_count(self) -> int:
        return sum(1 for r in self.records if r.status == STATUS_DONE)

    def statements_done(self) -> int:
        return sum(r.statements for r in self.records if r.status == STATUS_DONE)

    def all_done(self) -> bool:
        return all(r.status == STATUS_DONE for r in self.records)

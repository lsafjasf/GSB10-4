"""Resumable chunk execution with retry classification."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .checkpoint import (
    STATUS_DONE,
    STATUS_FAILED_FATAL,
    STATUS_FAILED_RETRYABLE,
    CheckpointStore,
)
from .chunker import Chunk, split_and_chunk

__all__ = [
    "ChunkExecutor",
    "RetryPolicy",
    "ChunkResult",
    "ExecutionSummary",
    "RetryableError",
    "CrashInjected",
    "make_sqlite_executor",
]


class RetryableError(Exception):
    """Wrap an exception to force the retryable classification."""


class CrashInjected(Exception):
    """Raised by crash hooks in tests to simulate process death."""


def _default_retryable_check(exc: BaseException) -> bool:
    if isinstance(exc, RetryableError):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        return any(
            token in msg
            for token in (
                "database is locked",
                "database table is locked",
                "database is busy",
                "interrupted",
            )
        )
    return False


@dataclass
class RetryPolicy:
    max_retries: int = 3
    backoff_base: float = 0.05
    backoff_max: float = 5.0
    retryable_check: Callable[[BaseException], bool] = _default_retryable_check

    def backoff(self, retry_no: int) -> float:
        return min(self.backoff_max, self.backoff_base * (2 ** retry_no))


@dataclass
class ChunkResult:
    index: int
    status: str
    attempts: int
    retries: int
    error_type: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class ExecutionSummary:
    status: str  # done | failed_retryable | failed_fatal
    checkpoint_path: str
    chunks_total: int
    chunks_done: int
    statements_done: int
    statements_total: int
    results: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "checkpoint_path": self.checkpoint_path,
            "chunks_total": self.chunks_total,
            "chunks_done": self.chunks_done,
            "statements_done": self.statements_done,
            "statements_total": self.statements_total,
            "results": [r.__dict__ for r in self.results],
        }


def make_sqlite_executor(db_path: str):
    """Return ``(run_chunk, connection)`` executing chunks transactionally.

    Each chunk runs inside BEGIN/COMMIT; any failure rolls the whole chunk
    back, so a chunk's side effects are all-or-nothing and a retry never
    replays partial effects. (sqlite3 DDL is transactional; statements such
    as VACUUM that cannot run inside a transaction are unsupported.)
    """

    conn = sqlite3.connect(db_path)
    conn.isolation_level = None

    def run_chunk(chunk: Chunk) -> None:
        conn.execute("BEGIN")
        try:
            for stmt in chunk.statements:
                if not stmt.has_code:
                    continue
                conn.execute(stmt.text.decode("utf-8"))
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    return run_chunk, conn


class ChunkExecutor:
    def __init__(
        self,
        script: bytes,
        checkpoint_path: str,
        run_chunk: Callable[[Chunk], None],
        chunk_size: int = 1 << 20,
        policy: Optional[RetryPolicy] = None,
        crash_hook: Optional[Callable[[int], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.script = script
        self.checkpoint_path = checkpoint_path
        self.run_chunk = run_chunk
        self.chunk_size = chunk_size
        self.policy = policy or RetryPolicy()
        self.crash_hook = crash_hook
        self.sleep = sleep
        self.chunks = split_and_chunk(script, chunk_size)

    def run(self) -> ExecutionSummary:
        store = CheckpointStore.open_or_create(
            self.checkpoint_path, self.script, self.chunks, self.chunk_size
        )
        results: list[ChunkResult] = []
        start = store.next_pending()
        if start is None:
            return self._summary(store, STATUS_DONE, results)

        first = store.records[start]
        if first.status == STATUS_FAILED_FATAL:
            results.append(self._result_of(first))
            return self._summary(store, STATUS_FAILED_FATAL, results)

        status = STATUS_DONE
        for chunk in self.chunks[start:]:
            rec = store.records[chunk.index]
            attempt_ok = False
            last_exc: Optional[BaseException] = None
            while True:
                store.mark_started(chunk.index)
                try:
                    self.run_chunk(chunk)
                except CrashInjected:
                    # Simulated process death: nothing is recorded, exactly
                    # like a power loss between statements.
                    raise
                except BaseException as exc:  # noqa: BLE001 - classified below
                    last_exc = exc
                    retryable = self.policy.retryable_check(exc)
                    if retryable and rec.attempts <= self.policy.max_retries:
                        self.sleep(self.policy.backoff(rec.attempts))
                        continue
                    store.mark_failed(chunk.index, exc, retryable)
                    results.append(self._result_of(store.records[chunk.index]))
                    status = (
                        STATUS_FAILED_RETRYABLE if retryable else STATUS_FAILED_FATAL
                    )
                    return self._summary(store, status, results)
                store.mark_done(chunk.index)
                attempt_ok = True
                break
            results.append(self._result_of(store.records[chunk.index]))
            if self.crash_hook is not None:
                self.crash_hook(chunk.index)
        return self._summary(store, status, results)

    @staticmethod
    def _result_of(rec) -> ChunkResult:
        return ChunkResult(
            index=rec.index,
            status=rec.status,
            attempts=rec.attempts,
            retries=rec.retries,
            error_type=rec.error_type,
            error_message=rec.error_message,
        )

    def _summary(self, store: CheckpointStore, status: str, results):
        return ExecutionSummary(
            status=status,
            checkpoint_path=self.checkpoint_path,
            chunks_total=len(self.chunks),
            chunks_done=store.done_count(),
            statements_done=store.statements_done(),
            statements_total=sum(c.statement_count for c in self.chunks),
            results=results,
        )

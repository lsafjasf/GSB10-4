"""分块执行器：断点续跑、可重试/不可重试失败分类、副作用不重放。"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, List

from .chunker import Chunk, build_chunks
from .checkpoint import Checkpoint


class RetryableError(Exception):
    """可重试错误（如锁冲突、临时网络抖动）。"""


class FatalError(Exception):
    """不可重试错误（如语法错误、约束冲突），立即终止整轮执行。"""


@dataclass
class RunReport:
    status: str  # done | failed_retryable | failed_fatal
    executed_chunks: List[int] = field(default_factory=list)
    failed_chunk: int | None = None
    attempts: dict = field(default_factory=dict)  # chunk_index -> attempts


class Runner:
    def __init__(
        self,
        script: str,
        checkpoint_path: str,
        executor: Callable[[str, int], None],
        max_chunk_bytes: int = 1 << 20,
        max_retries: int = 3,
        retry_backoff: float = 0.05,
    ):
        self.script = script
        self.chunks: List[Chunk] = build_chunks(script, max_chunk_bytes)
        self.executor = executor
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        if os.path.exists(checkpoint_path):
            self.checkpoint = Checkpoint.load(checkpoint_path, script, self.chunks)
        else:
            self.checkpoint = Checkpoint(checkpoint_path, script, self.chunks)
            self.checkpoint.save()

    def run(self) -> RunReport:
        start = self.checkpoint.next_pending_index()
        report = RunReport(status="done")
        for chunk in self.chunks[start:]:
            idx = chunk.index
            attempt = 0
            while True:
                attempt += 1
                self.checkpoint.mark_attempt(idx)
                try:
                    self.executor(chunk.text, idx)
                except RetryableError as e:
                    if attempt <= self.max_retries:
                        time.sleep(self.retry_backoff)
                        continue
                    self.checkpoint.mark_failed(idx, retryable=True, error=str(e))
                    report.status = "failed_retryable"
                    report.failed_chunk = idx
                    report.attempts[idx] = attempt
                    return report
                except FatalError as e:
                    self.checkpoint.mark_failed(idx, retryable=False, error=str(e))
                    report.status = "failed_fatal"
                    report.failed_chunk = idx
                    report.attempts[idx] = attempt
                    return report
                else:
                    self.checkpoint.mark_done(idx)
                    report.executed_chunks.append(idx)
                    report.attempts[idx] = attempt
                    break
        return report

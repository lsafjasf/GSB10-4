"""断点文件：原子写、可校验、损坏即拒绝续跑（宁可重跑也不重放副作用）。

格式（JSON，UTF-8）：
{
  "version": 1,
  "script_sha256": "<整脚本 hex>",
  "max_chunk_bytes": 1048576,
  "chunk_count": 12,
  "chunks": [
    {"index": 0, "stmt_count": 40, "byte_size": 10231,
     "sha256": "<块文本 hex>", "status": "done",
     "attempts": 1, "error": null},
    ...
  ]
}
status ∈ pending | done | failed_retryable | failed_fatal
attempts 记录该块已尝试次数（含成功那次）。
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import List

VERSION = 1
STATUSES = {"pending", "done", "failed_retryable", "failed_fatal"}


class CheckpointCorruptedError(RuntimeError):
    pass


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Checkpoint:
    def __init__(self, path: str, script: str, chunks):
        self.path = path
        self.script_sha256 = _sha256(script)
        self.data = {
            "version": VERSION,
            "script_sha256": self.script_sha256,
            "chunk_count": len(chunks),
            "chunks": [
                {
                    "index": c.index,
                    "stmt_count": c.stmt_count,
                    "byte_size": c.byte_size,
                    "sha256": _sha256(c.text),
                    "status": "pending",
                    "attempts": 0,
                    "error": None,
                }
                for c in chunks
            ],
        }

    # ---- 持久化 ----
    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)  # 原子替换

    @classmethod
    def load(cls, path: str, script: str, chunks) -> "Checkpoint":
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise CheckpointCorruptedError(f"checkpoint unreadable: {e}") from e

        obj = cls.__new__(cls)
        obj.path = path
        obj.data = data
        obj.script_sha256 = _sha256(script)
        obj._validate(chunks)
        return obj

    def _validate(self, chunks):
        d = self.data
        if not isinstance(d, dict) or d.get("version") != VERSION:
            raise CheckpointCorruptedError("bad version or not an object")
        if d.get("script_sha256") != self.script_sha256:
            raise CheckpointCorruptedError("script hash mismatch: 脚本已变更，拒绝续跑")
        records = d.get("chunks")
        if not isinstance(records, list) or len(records) != len(chunks):
            raise CheckpointCorruptedError("chunk list length mismatch")
        for rec, chunk in zip(records, chunks):
            if (
                rec.get("index") != chunk.index
                or rec.get("sha256") != _sha256(chunk.text)
                or rec.get("stmt_count") != chunk.stmt_count
                or rec.get("status") not in STATUSES
                or not isinstance(rec.get("attempts"), int)
            ):
                raise CheckpointCorruptedError(
                    f"chunk {rec.get('index')} record mismatch or invalid"
                )

    # ---- 运行期接口 ----
    def next_pending_index(self) -> int:
        for rec in self.data["chunks"]:
            if rec["status"] != "done":
                return rec["index"]
        return len(self.data["chunks"])

    def mark_done(self, index: int):
        rec = self.data["chunks"][index]
        rec["status"] = "done"
        rec["error"] = None
        self.save()

    def mark_attempt(self, index: int):
        self.data["chunks"][index]["attempts"] += 1
        self.save()

    def mark_failed(self, index: int, retryable: bool, error: str):
        rec = self.data["chunks"][index]
        rec["status"] = "failed_retryable" if retryable else "failed_fatal"
        rec["error"] = error
        self.save()

    def records(self) -> List[dict]:
        return self.data["chunks"]

    def byte_size(self) -> int:
        return os.path.getsize(self.path)

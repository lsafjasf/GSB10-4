"""消费位点批量提交 / 回退库（仅标准库）。

核心规则
========
位点为严格递增的非负整数 0,1,2,...；committed 表示"已连续提交"的最高位点，
初始 -1 表示一个位点都还没提交。

* mark_done(off)：位点 off 已处理完成，先放进 pending（已完成但未提交）集合。
* advance()：从 committed+1 开始，只要下一位点在 pending 中就连续推进；
  一旦遇到空洞（下一位点未完成）立即停止。空洞后面的位点永远不会被跳过。
* 批量提交 = mark_done(一批) + advance()，与逐条提交在最终 committed 上严格一致。

处理与提交解耦
==============
处理线程只负责 mark_done（结果先落 WAL）；提交由独立步骤 advance + 落盘。
进程被杀后 recover() 重放 WAL：已 COMMIT 的位点之前全部视为完成，
COMMIT 之后已写 DONE 记录的位点进入 pending，未写 DONE 记录的位点重新处理，
因此重启后不会跳过任何未处理位点（at-least-once 语义）。

失败回退
========
用 BatchGuard 包住一批处理：批中任何一位点失败，rollback() 只撤销本批
"已标记完成但尚未提交"的位点，已进入连续前缀（已提交）的位点不可回退
（需要重放/补偿，由上层处理），回退范围最小。
"""

from __future__ import annotations

import os
from typing import Iterable, List, Optional, Tuple


class OffsetTracker:
    """连续前缀位点追踪器（纯内存逻辑，无 IO）。"""

    def __init__(self, start: int = -1):
        self.committed = start
        # 已完成、且严格大于 committed 的位点（即"空洞之后"的完成位点）。
        self._pending: set = set()
        # 统计用：pending 历史最大规模（空洞上界）。
        self.max_pending = 0

    @property
    def pending(self) -> set:
        return self._pending

    def mark_done(self, offset: int) -> None:
        if offset <= self.committed:
            # 已在连续前缀内，重复完成幂等忽略。
            return
        if offset < 0:
            raise ValueError("offset must be >= 0")
        self._pending.add(offset)
        if len(self._pending) > self.max_pending:
            self.max_pending = len(self._pending)

    def mark_done_batch(self, offsets: Iterable[int]) -> List[int]:
        newly = []
        for off in offsets:
            if off > self.committed and off not in self._pending:
                newly.append(off)
            self.mark_done(off)
        return newly

    def advance(self) -> List[int]:
        """沿连续前缀尽量推进，返回本次提交的位点序列（严格升序）。

        空洞（committed+1 未完成）时返回空/前缀片段并停在空洞之前。
        """
        advanced: List[int] = []
        nxt = self.committed + 1
        while nxt in self._pending:
            self._pending.remove(nxt)
            advanced.append(nxt)
            nxt += 1
        if advanced:
            self.committed = advanced[-1]
        return advanced

    def commit_batch(self, offsets: Iterable[int]) -> Tuple[int, List[int]]:
        """批量标记完成 + 推进，返回 (新 committed, 本次推进位点序列)。"""
        self.mark_done_batch(offsets)
        advanced = self.advance()
        return self.committed, advanced

    def is_done(self, offset: int) -> bool:
        return offset <= self.committed or offset in self._pending


class BatchGuard:
    """一批处理的最小回退守卫。

    用法：
        guard = BatchGuard(tracker)
        guard.mark_done(off)   # 每个位点处理成功后调用
        ... 中途失败 ...
        rolled, committed = guard.rollback()
    """

    def __init__(self, tracker: OffsetTracker):
        self.tracker = tracker
        self._marked: List[int] = []
        self._rolled_back = False

    def mark_done(self, offset: int) -> None:
        if self._rolled_back:
            raise RuntimeError("batch already rolled back")
        self.tracker.mark_done(offset)
        self._marked.append(offset)

    def rollback(self) -> Tuple[List[int], List[int]]:
        """撤销本批标记。

        返回 (rolled_back, already_committed)：
        rolled_back     —— 成功撤销、回到"未处理"状态的位点（范围最小）；
        already_committed —— 已进入连续前缀、无法在位点层回退的位点
                            （提交与处理解耦时正常应为空）。
        """
        self._rolled_back = True
        rolled_back: List[int] = []
        already_committed: List[int] = []
        seen = set()
        for off in self._marked:
            if off in seen:
                continue
            seen.add(off)
            if off in self.tracker._pending:
                self.tracker._pending.remove(off)
                rolled_back.append(off)
            elif off <= self.tracker.committed:
                already_committed.append(off)
        return rolled_back, already_committed


class OffsetStore:
    """WAL：D <off> 表示处理完成，C <off> 表示连续前缀已提交到 off。

    提交与处理解耦：处理只追加 D；提交方 advance 后追加 C。
    recover() 重放日志重建 tracker，保证不跳过未处理位点。
    """

    def __init__(self, path: str, sync: bool = False):
        self.path = path
        self.sync = sync
        self._fp = open(path, "a+", buffering=1)  # 行缓冲，崩溃不撕裂半行的处理见下

    def _append(self, line: str) -> None:
        self._fp.write(line + "\n")
        self._fp.flush()
        if self.sync:
            os.fsync(self._fp.fileno())

    def log_done(self, offset: int) -> None:
        self._append("D %d" % offset)

    def log_commit(self, offset: int) -> None:
        self._append("C %d" % offset)

    def close(self) -> None:
        self._fp.close()

    def __enter__(self) -> "OffsetStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @classmethod
    def replay(cls, path: str) -> Tuple[OffsetTracker, List[str]]:
        """重放 WAL，返回 (tracker, 被丢弃的损坏尾行)。"""
        committed = -1
        done_after: List[int] = []
        garbage: List[str] = []
        if os.path.exists(path):
            with open(path, "rb") as fp:
                raw = fp.read()
            for line in raw.split(b"\n"):
                if not line:
                    continue
                try:
                    kind, val = line.split()
                    off = int(val)
                except Exception:
                    garbage.append(line.decode("utf-8", "replace"))
                    continue  # kill -9 撕裂的半行直接丢弃
                if kind == b"C":
                    committed = max(committed, off)
                elif kind == b"D":
                    if off > committed:
                        done_after.append(off)
        tracker = OffsetTracker(committed)
        tracker.mark_done_batch(done_after)
        tracker.advance()  # 提交日志可能落后于 DONE 日志，重放时补齐连续前缀
        return tracker, garbage

    @classmethod
    def recover(cls, path: str, sync: bool = False) -> Tuple[OffsetTracker, OffsetStore, List[str]]:
        tracker, garbage = cls.replay(path)
        store = cls(path, sync=sync)
        store.log_commit(tracker.committed)  # 固化重放补出的前缀
        return tracker, store, garbage

    def compact(self, tracker: OffsetTracker) -> None:
        """日志压缩：只保留当前 C 前缀与 pending 位点。"""
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fp:
            fp.write("C %d\n" % tracker.committed)
            for off in sorted(tracker._pending):
                fp.write("D %d\n" % off)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, self.path)
        self._fp.close()
        self._fp = open(self.path, "a+", buffering=1)

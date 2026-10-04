"""消费位点批量提交与回退库（仅标准库）。

核心模型
--------
- 位点(offset)是非负整数，严格递增，单条消息只对应一个位点。
- ``base`` = 已连续提交位点的下一个位置（高水位），即 [0, base) 全部处理完。
- 完成但尚未连续的位点用有序的“不相交区间列表”暂存。
- 只有当某个区间的起点恰好等于 base 时才能推进 base；遇到空洞立刻停止。
- 因此：无论完成通知以什么顺序到达，base 的每一次推进都等价于逐条提交。
"""

from __future__ import annotations

import os
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Callable, List, Optional


class OffsetTable:
    """内存位点表：高水位 + 有序不相交区间。

    不变量：``intervals`` 中每个 ``[start, end]`` 都满足 ``start >= base``，
    相邻区间互不相邻（起点大于前一段 end+1），整体按 start 升序。
    """

    __slots__ = ("base", "intervals")

    def __init__(self, base: int = 0) -> None:
        self.base = base
        self.intervals: List[List[int]] = []

    def mark_done(self, offset: int) -> bool:
        """登记一个已完成位点。

        - offset < base（已提交）：忽略，返回 False。
        - 落在已有区间内（重复完成）：忽略，返回 False。
        - 与左右区间相邻则合并，否则插入新区间。
        """
        if offset < self.base:
            return False
        iv = self.intervals
        pos = bisect_left(iv, [offset, offset])
        if pos > 0 and iv[pos - 1][1] >= offset:
            return False  # 已在左侧区间内
        if pos < len(iv) and iv[pos][0] == offset:
            return False  # 已在右侧区间内（起点恰为 offset）
        merge_left = pos > 0 and iv[pos - 1][1] == offset - 1
        merge_right = pos < len(iv) and iv[pos][0] == offset + 1
        if merge_left and merge_right:
            iv[pos - 1][1] = iv[pos][1]
            del iv[pos]
        elif merge_left:
            iv[pos - 1][1] = offset
        elif merge_right:
            iv[pos][0] = offset
        else:
            iv.insert(pos, [offset, offset])
        return True

    def advance(self) -> int:
        """沿连续前缀推进高水位，遇空洞即停，返回推进后的 base。"""
        iv = self.intervals
        base = self.base
        k = 0
        while k < len(iv) and iv[k][0] <= base:
            if iv[k][1] >= base:
                base = iv[k][1] + 1
            k += 1
        if k:
            del iv[:k]
        self.base = base
        return self.base

    def pending_prefix(self, limit: Optional[int] = None) -> List[int]:
        """当前可以连续提交的位点序列（仅供观察/测试）。"""
        iv = self.intervals
        if not iv or iv[0][0] != self.base:
            return []
        end = iv[0][1]
        if limit is not None:
            end = min(end, self.base + limit - 1)
        return list(range(self.base, end + 1))

    def hole(self) -> Optional[int]:
        """已知完成区间中的第一个空洞位点；无空洞时返回 None。"""
        iv = self.intervals
        if not iv:
            return None
        if iv[0][0] > self.base:
            return self.base
        return None if len(iv) == 1 else iv[0][1] + 1


@dataclass
class BatchResult:
    """一批位点的处理结果（回退数据）。"""

    ok: bool
    committed: List[int] = field(default_factory=list)   # 成功并入位点表的位点
    rolled_back: List[int] = field(default_factory=list)  # 失败前已处理、被整批回退的位点
    failed_at: Optional[int] = None                      # 触发失败的位点
    remaining: List[int] = field(default_factory=list)    # 失败位点之后尚未处理的位点
    error: Optional[BaseException] = None


def run_batch(table: OffsetTable, offsets: List[int],
              handler: Callable[[int], None]) -> BatchResult:
    """串行处理一批位点，全部成功后才并入位点表。

    解耦点：handler 处理期间不修改 table，结果先放在 staged 暂存区。
    - 全部成功：staged 并入 table 并推进高水位。
    - 任一失败：staged 全部回退（table 完全不变），回退范围只含本批
      已处理的 staged 部分，不触碰之前批次已经推进的位点。
    """
    staged: List[int] = []
    for idx, offset in enumerate(offsets):
        try:
            handler(offset)
        except Exception as exc:  # 业务失败：回退到失败之前
            return BatchResult(
                ok=False,
                rolled_back=list(staged),
                failed_at=offset,
                remaining=list(offsets[idx + 1:]),
                error=exc,
            )
        staged.append(offset)
    for offset in staged:
        table.mark_done(offset)
    table.advance()
    return BatchResult(ok=True, committed=list(staged))


class WalCommitter:
    """WAL 持久化提交器：处理与提交解耦，崩溃后从日志恢复。

    日志行格式（文本，便于排查）::

        D <offset>   某位点处理完成（先处理、成功后才写 D，fsync）
        C <base>     高水位推进到 base（连续前缀提交点，fsync）

    最后一行若没有换行符，视为崩溃瞬间的半行，恢复时丢弃。
    语义：D 必须在业务处理成功之后写 => 重启最多重复处理，绝不跳过。
    """

    def __init__(self, path: str, base: int = 0, fsync: bool = True) -> None:
        self.path = path
        self.fsync = fsync
        self.table = OffsetTable(base)
        self._fh = open(path, "a", buffering=1, encoding="utf-8")

    def _append(self, line: str) -> None:
        self._fh.write(line)
        if self.fsync:
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def mark_done(self, offset: int) -> bool:
        self._append(f"D {offset}\n")
        return self.table.mark_done(offset)

    def mark_done_batch(self, offsets, commit: bool = True) -> int:
        """批量提交：多条 D 记录一次 fsync，可选随后推进高水位。"""
        self._fh.write("".join(f"D {off}\n" for off in offsets))
        if self.fsync:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        for offset in offsets:
            self.table.mark_done(offset)
        return self.commit() if commit else self.table.base

    def commit(self) -> int:
        new_base = self.table.advance()
        self._append(f"C {new_base}\n")
        return new_base

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "WalCommitter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @classmethod
    def recover(cls, path: str, fsync: bool = True) -> "WalCommitter":
        """重放 WAL 恢复；未提交（不连续）的 D 记录恢复回暂存区间。"""
        inst = cls(path, 0, fsync)
        if not os.path.exists(path):
            return inst
        with open(path, "rb") as fh:
            raw = fh.read()
        if raw and not raw.endswith(b"\n"):
            cut = raw.rfind(b"\n")
            raw = raw[: cut + 1] if cut >= 0 else b""
        for line in raw.decode("utf-8").splitlines():
            op, _, arg = line.partition(" ")
            if op == "D":
                inst.table.mark_done(int(arg))
            elif op == "C":
                base = int(arg)
                inst.table.base = base
                inst.table.intervals = [
                    iv for iv in inst.table.intervals if iv[1] >= base
                ]
        return inst

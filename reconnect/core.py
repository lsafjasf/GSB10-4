"""断线重连后的状态重建：续传(resume) vs 全量重放(full replay)。

判定三要素（客户端 checkpoint 携带，服务端校验）：
  1. version  状态基线版本号。服务端整体更换状态来源（换快照/重建）时 +1，
              旧版本的所有位点与摘要一律作废。
  2. seq      位点。客户端已应用到的最大事件序号。服务端保留
              (base_seq, latest_seq] 区间的事件及逐位点摘要。
  3. digest   状态摘要。客户端在 checkpoint 位点处的状态哈希，
              服务端用同位点摘要做一致性比对。

安全原则：三要素任一不可用 / 不一致 / 已过期，一律退化为全量重放，绝不猜着续。

性能设计：摘要是逐键哈希的 XOR 累积值，单条事件 O(1) 增量更新，
与状态规模无关；全量重算 digest_of() 仅在快照/校验时使用。
"""
from __future__ import annotations

import bisect
import copy
import hashlib
from dataclasses import dataclass

_MISSING = object()  # 键不存在哨兵


# ---------------------------------------------------------------- 状态与摘要

def _pair_hash(key: str, value) -> int:
    h = hashlib.blake2b(digest_size=16)
    h.update(key.encode("utf-8"))
    h.update(b"\x00")
    h.update(repr(value).encode("utf-8"))
    return int.from_bytes(h.digest(), "big")


def digest_of(state: dict) -> str:
    """确定性状态摘要：逐键哈希的 XOR 累积，与枚举顺序无关。"""
    acc = 0
    for key, value in state.items():
        acc ^= _pair_hash(key, value)
    return f"{acc:032x}"


def digest_apply(digest: str, key: str, old, new) -> str:
    """单键变更后的摘要增量更新，O(1)。"""
    acc = int(digest, 16)
    if old is not _MISSING:
        acc ^= _pair_hash(key, old)
    if new is not _MISSING:
        acc ^= _pair_hash(key, new)
    return f"{acc:032x}"


@dataclass(frozen=True)
class Op:
    seq: int
    kind: str            # 'set' | 'inc' | 'del'
    key: str
    value: object = None


def apply_op(state: dict, op: Op):
    """应用事件，返回 (key, old, new) 供增量摘要使用。"""
    old = state.get(op.key, _MISSING)
    if op.kind == "set":
        state[op.key] = op.value
    elif op.kind == "inc":
        state[op.key] = state.get(op.key, 0) + op.value
    elif op.kind == "del":
        state.pop(op.key, None)
    else:
        raise ValueError(f"unknown op kind: {op.kind!r}")
    return op.key, old, state.get(op.key, _MISSING)


@dataclass(frozen=True)
class Checkpoint:
    """客户端在断线前最后提交的位点。"""
    version: int
    seq: int
    digest: str


# ---------------------------------------------------------------- 判定结果

@dataclass(frozen=True)
class Resume:
    """续传：只需补发 (client_seq, latest_seq] 的事件。"""
    events: tuple
    final_digest: str
    reason: str = "ok"


@dataclass(frozen=True)
class FullReplay:
    """全量重放：整体快照 + 当前位点，客户端全量替换。"""
    snapshot: dict
    version: int
    seq: int
    digest: str
    reason: str


# ---------------------------------------------------------------- 服务端

class Server:
    """事件源 + 状态持有者。保留 (base_seq, latest_seq] 的事件与逐位点摘要。"""

    def __init__(self) -> None:
        self.version = 0
        self.state: dict = {}
        self._events: list[Op] = []          # seq ∈ (base_seq, latest_seq]
        self.base_seq = 0
        self.latest_seq = 0
        self._digest = digest_of(self.state)          # 当前状态摘要, O(1) 增量维护
        self._digests: dict[int, str] = {0: self._digest}  # 逐位点摘要

    # ---- 正常业务 ----

    def publish(self, kind: str, key: str, value=None) -> Op:
        self.latest_seq += 1
        op = Op(self.latest_seq, kind, key, value)
        k, old, new = apply_op(self.state, op)
        self._digest = digest_apply(self._digest, k, old, new)
        self._events.append(op)
        self._digests[op.seq] = self._digest
        return op

    # ---- 运维操作（位点过期 / 版本更替 / 摘要丢失的来源） ----

    def compact(self, keep_last: int) -> None:
        """日志截断：只保留最近 keep_last 条事件，更早的位点过期。"""
        cut = max(self.base_seq, self.latest_seq - max(keep_last, 0))
        self._events = [e for e in self._events if e.seq > cut]
        self._digests = {s: d for s, d in self._digests.items() if s >= cut}
        self.base_seq = cut

    def rotate_version(self) -> None:
        """状态基线整体更换（换快照源/重建）：旧版本位点全部失效。"""
        self.version += 1
        self._events = []
        self.base_seq = self.latest_seq
        self._digests = {self.latest_seq: self._digest}

    def lose_digests(self) -> None:
        """模拟崩溃后摘要丢失：只剩基线位点的摘要，其余位点摘要不可考。"""
        self._digests = {self.base_seq: self._digests.get(self.base_seq, self._digest)}

    # ---- 重连判定 ----

    def decide(self, cp: Checkpoint):
        """纯函数：同一 checkpoint 多次重连得到同一判定（交错重连安全）。"""
        if cp.version < 0:
            return self._full("initial-sync")
        if cp.version != self.version:
            return self._full("version-mismatch")
        if cp.seq > self.latest_seq:
            return self._full("seq-ahead-of-server")
        if cp.seq < self.base_seq:
            return self._full("seq-expired")
        expected = self._digests.get(cp.seq)
        if expected is None:
            return self._full("digest-unavailable")
        if cp.digest != expected:
            return self._full("digest-mismatch")
        idx = bisect.bisect_right(self._events, cp.seq, key=lambda e: e.seq)
        return Resume(events=tuple(self._events[idx:]), final_digest=self._digest)

    def _full(self, reason: str) -> FullReplay:
        return FullReplay(
            snapshot=copy.deepcopy(self.state),
            version=self.version,
            seq=self.latest_seq,
            digest=self._digest,
            reason=reason,
        )


# ---------------------------------------------------------------- 客户端

class ReconnectDropped(Exception):
    """重连传输途中再次断开。已投递的部分批次绝不提交。"""

    def __init__(self, delivered: int, total: int) -> None:
        super().__init__(f"dropped after {delivered}/{total} events")
        self.delivered = delivered
        self.total = total


class StateCorrupted(Exception):
    """续传应用完毕后摘要与服务端不一致：说明丢/重，必须告警。"""


class Client:
    """状态副本。所有提交都是原子的：要么整批生效，要么原位不动。"""

    def __init__(self) -> None:
        self.state: dict = {}
        self.checkpoint = Checkpoint(version=-1, seq=0, digest=digest_of({}))

    def sync(self, server: Server, deliver: int | None = None):
        """一次重连同步。

        deliver: 模拟传输途中再次断开——只投递前 deliver 条事件就断线。
                 部分批次不提交，checkpoint 不变，下次重连安全重试。
        返回本次判定（Resume 或 FullReplay）。
        """
        decision = server.decide(self.checkpoint)

        if isinstance(decision, FullReplay):
            if deliver is not None and deliver < 1:
                raise ReconnectDropped(0, 1)   # 快照没传完，什么都不改
            self.state = copy.deepcopy(decision.snapshot)
            self.checkpoint = Checkpoint(decision.version, decision.seq, decision.digest)
            return decision

        events = decision.events
        total = len(events)
        n = total if deliver is None else min(deliver, total)

        staging = dict(self.state)             # 先落在暂存区
        digest = self.checkpoint.digest        # 从已校验的摘要增量推进
        for op in events[:n]:
            k, old, new = apply_op(staging, op)
            digest = digest_apply(digest, k, old, new)
        if n < total:
            raise ReconnectDropped(n, total)   # 未提交：state/checkpoint 原样

        if digest != decision.final_digest:    # 端到端不重不漏校验
            raise StateCorrupted(
                f"resume digest mismatch at seq {server.latest_seq}")
        self.state = staging                   # 原子提交
        self.checkpoint = Checkpoint(server.version, server.latest_seq, digest)
        return decision

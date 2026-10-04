# -*- coding: utf-8 -*-
"""断线重连后的状态重建库（仅依赖 Python 3 标准库）。

判定"续传 vs 全量"的三项依据
----------------------------
1. 位点 offset（必须保留）：
   单调递增事件序号，客户端记录已应用的最后一个 seq。服务端只保留有限
   回放窗口 [base_seq+1, seq]。位点落在窗口外（过期 / 超前）则无法续传。
2. 版本号 version（必须一致）：
   服务端状态世代号。服务端重置 / 快照压缩 / 换数据源时 version+1，
   跨版本的位点不可比较，必须全量。
3. 状态摘要 digest（必须吻合）：
   对状态字典的顺序无关增量哈希（blake2b-64 元素级哈希的模 2^64 和），
   客户端在 offset 处的摘要必须等于服务端在同一 offset 处的摘要；
   不一致说明本地状态已被污染（丢更新 / 重复应用 / 本地写坏）。

安全原则：三项依据中任何一项缺失、不可用或已过期，一律退化为全量，
绝不猜着续。全量路径用"快照 + 快照之后的增量"整体替换本地状态。
"""
from __future__ import annotations

import hashlib
from collections import namedtuple

MASK64 = (1 << 64) - 1

# 操作序列；一条事件对应状态机的一次确定性迁移。
Event = namedtuple("Event", ["seq", "op", "key", "value"])
# 客户端重连时携带的续传凭据。
ResumeToken = namedtuple("ResumeToken", ["offset", "version", "digest"])
Decision = namedtuple("Decision", ["kind", "offset", "reason"])


class ConnectionDropped(Exception):
    """连接在握手或流传输过程中断开（含"所需事件在传输中被清理"）。"""


class ProtocolError(Exception):
    """序号不连续 / 续传起点不符等"不重不漏"被破坏的情形。"""


def entry_hash(key, value) -> int:
    """单条 (key, value) 记录的确定性 64 位哈希。

    不用 Python 内置 hash（进程级随机化）；blake2b 为标准库内置 C 实现，
    固定 8 字节摘要，跨进程可复现。
    """
    payload = repr(key).encode("utf-8") + b"\x00" + repr(value).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(),
                          "big")


class StateStore:
    """权威状态：dict + 顺序无关增量摘要。

    摘要 = Σ entry_hash(key, value) mod 2^64，
    set 时"减旧值哈希 + 加新值哈希"，del 时"减旧值哈希"，
    因此单事件迁移 O(1)，且与处理顺序无关。
    """

    def __init__(self):
        self.data = {}
        self.digest = 0

    def apply(self, op, key, value=None):
        old = self.data.get(key)
        if key in self.data:
            self.digest = (self.digest - entry_hash(key, old)) & MASK64
        if op == "set":
            self.data[key] = value
            self.digest = (self.digest + entry_hash(key, value)) & MASK64
        elif op == "del":
            self.data.pop(key, None)
        else:
            raise ValueError("unknown op: %r" % op)

    def recompute_digest(self) -> int:
        """独立全量重算摘要，用于自测对拍。"""
        d = 0
        for k, v in self.data.items():
            d = (d + entry_hash(k, v)) & MASK64
        return d


class Server:
    """带有限回放窗口与版本世代的事件源（服务端模拟）。"""

    def __init__(self, retention: int = 1000):
        self.retention = retention
        self.version = 1
        self.seq = 0
        # log[i] 的 seq == base_seq + 1 + i；digest_log[i] 是应用该事件后的摘要。
        self.log = []
        self.digest_log = []
        self.base_seq = 0          # 所有 seq <= base_seq 的事件已被清理
        self.base_digest = 0       # base_seq 时刻的状态摘要
        self.state = StateStore()

    # ---- 服务端侧生产事件 -------------------------------------------------
    def publish(self, op, key, value=None) -> Event:
        self.seq += 1
        self.state.apply(op, key, value)
        ev = Event(self.seq, op, key, value)
        self.log.append(ev)
        self.digest_log.append(self.state.digest)
        self._trim()
        return ev

    def _trim(self):
        """超过 2 倍保留窗口时批量裁剪（摊销 O(1)）。"""
        if len(self.log) <= 2 * self.retention:
            return
        drop = len(self.log) - self.retention
        last = self.log[drop - 1]
        self.base_seq = last.seq
        self.base_digest = self.digest_log[drop - 1]
        del self.log[:drop]
        del self.digest_log[:drop]

    def bump_version(self):
        """模拟服务端重置 / 快照压缩：旧版本位点全部作废。"""
        self.version += 1
        self.base_seq = self.seq
        self.base_digest = self.state.digest
        self.log.clear()
        self.digest_log.clear()

    # ---- 续传判定 ---------------------------------------------------------
    def digest_at(self, offset: int):
        if offset == self.base_seq:
            return self.base_digest
        if self.base_seq < offset <= self.seq:
            return self.digest_log[offset - self.base_seq - 1]
        return None

    def decide(self, token):
        """判定表的代码化实现，返回 Decision(kind, offset, reason)。"""
        if token is None:
            return Decision("full", self.seq, "无续传凭据，首次连接")
        if token.version != self.version:
            return Decision("full", self.seq, "版本号不一致，跨世代位点不可比较")
        if token.offset > self.seq:
            return Decision("full", self.seq, "位点超前于服务端，凭据不可信")
        if token.offset < self.base_seq:
            return Decision("full", self.seq, "位点过期，所需事件已超出回放窗口")
        server_digest = self.digest_at(token.offset)
        if server_digest is None:
            return Decision("full", self.seq, "服务端无法给出位点摘要，依据不可用")
        if server_digest != token.digest:
            return Decision("full", self.seq, "状态摘要不一致，本地状态已被污染")
        return Decision("resume", token.offset, "位点/版本/摘要全部吻合，可续传")

    def connect(self, token, drop_after=None) -> "Session":
        return Session(self, self.decide(token), drop_after)


class Session:
    """一次连接。drop_after 表示投递 N 条事件后强制断开（模拟网络抖动）。"""

    def __init__(self, server: Server, decision: Decision, drop_after=None):
        self.server = server
        self.decision = decision
        self.drop_after = drop_after
        self._delivered = 0
        self._start = None

    def handshake(self):
        """返回 ('full', (snapshot_seq, snapshot_state, digest, version))
        或 ('resume', resume_offset)。"""
        if self.decision.kind == "full":
            seq = self.server.seq
            snap = (
                seq,
                dict(self.server.state.data),
                self.server.state.digest,
                self.server.version,
            )
            self._start = seq
            return "full", snap
        self._start = self.decision.offset
        return "resume", self.decision.offset

    def events(self):
        """从 _start+1 开始顺序投递；窗口被清理或达到 drop_after 则断开。"""
        seq = self._start
        while seq < self.server.seq:
            nxt = seq + 1
            if nxt <= self.server.base_seq:
                raise ConnectionDropped("所需事件在传输中被清理，需重新协商")
            if self.drop_after is not None and self._delivered >= self.drop_after:
                raise ConnectionDropped("模拟网络中断（已投递 %d 条）" % self._delivered)
            ev = self.server.log[nxt - self.server.base_seq - 1]
            self._delivered += 1
            yield ev
            seq = nxt


class Client:
    """断线重连消费者。只在摘要、版本、序号三重校验通过时续传，否则全量替换。"""

    def __init__(self, name="client"):
        self.name = name
        self.store = StateStore()
        self.offset = 0
        self.version = 0
        self.stats = {"resume": 0, "full": 0, "drops": 0, "attempts": 0}

    @property
    def state(self):
        return self.store.data

    def token(self):
        if self.version == 0:
            return None
        return ResumeToken(self.offset, self.version, self.store.digest)

    def sync_once(self, server: Server, drop_after=None):
        """尝试一次连接。成功追平返回 'resume'/'full'；中途断开返回 None。"""
        self.stats["attempts"] += 1
        session = server.connect(self.token(), drop_after=drop_after)
        try:
            kind, meta = session.handshake()
            if kind == "full":
                seq, snap, digest, version = meta
                # 全量路径：旧状态整体作废，不许在旧状态上叠加。
                new_store = StateStore()
                new_store.data = dict(snap)
                new_store.digest = digest
                self.store = new_store
                self.offset = seq
                self.version = version
                self.stats["full"] += 1
            else:
                if meta != self.offset:
                    raise ProtocolError(
                        "续传起点 %d 与本地位点 %d 不一致" % (meta, self.offset)
                    )
                self.stats["resume"] += 1
            for ev in session.events():
                # 不重不漏的硬校验：事件必须恰好接续在本地位点之后。
                if ev.seq != self.offset + 1:
                    raise ProtocolError(
                        "序号不连续：期望 %d，收到 %d" % (self.offset + 1, ev.seq)
                    )
                self.store.apply(ev.op, ev.key, ev.value)
                self.offset = ev.seq
            return kind
        except ConnectionDropped:
            # 已应用的事件不回滚（位点已推进，下次重连从新位点继续）。
            self.stats["drops"] += 1
            return None

    def sync(self, server: Server, drop_after=None, max_attempts=1000000):
        """反复重连直到追平；返回最后一次成功连接走的路径。"""
        result = None
        for _ in range(max_attempts):
            result = self.sync_once(server, drop_after=drop_after)
            if result is not None:
                return result
        raise RuntimeError("超过最大重连次数 %d" % max_attempts)

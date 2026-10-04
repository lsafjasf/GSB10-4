"""sticky.py -- 会话粘性 + 后端选择库（仅标准库）。

粘性规则（确定性，全部可复现）：
  1. 会话首次选择：一致性哈希环（MD5 虚拟节点环）上顺时针命中的第一个
     “健康且未超载”的后端；超载（load >= capacity）时沿环继续走。
  2. 命中后写入 pin 表；此后该会话永远走 pin，不再重新哈希。
  3. 后端摘除时，pin 表按策略处理：
       MIGRATE：沿环确定性迁移到下一个健康后端（两阶段拷贝，状态不丢）；
       REJECT ：会话拒绝，状态保留在原后端 WAL 中，恢复后归还。
  4. 后端恢复后，pin 优先于哈希环 —— 已迁走的会话不会被抢回；
     只有“首次绑定”的新会话才可能落到恢复的后端上。
  5. 迁移期间用 epoch fencing 挡住旧后端的迟到写入，保证不丢状态。
"""

from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

MASK64 = (1 << 64) - 1
VNODES_PER_BACKEND = 160          # 每个后端的虚拟节点数
MIGRATE = "migrate"               # 摘除策略：迁移
REJECT = "reject"                 # 摘除策略：拒绝
PIN_ACTIVE = "active"
PIN_REJECTED = "rejected"


def _h64(key: str) -> int:
    """确定性 64 位哈希（MD5 前 8 字节，大端）。"""
    return int.from_bytes(hashlib.md5(key.encode("utf-8")).digest()[:8], "big")


def _checksum(payload: Dict[str, Any]) -> str:
    """会话状态的规范化校验和（键序无关）。"""
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class StickyError(Exception):
    pass


class NoBackendError(StickyError):
    """没有任何健康后端可用。"""


class SessionRejectedError(StickyError):
    """REJECT 策略下会话被拒绝。"""


class StaleEpochError(StickyError):
    """旧后端持有过期 fencing token，写入被拒。"""


class HashRing:
    """MD5 虚拟节点一致性哈希环。"""

    def __init__(self, backend_ids: List[str], vnodes: int = VNODES_PER_BACKEND):
        self._points: List[int] = []
        self._owners: List[str] = []
        pts: List[Tuple[int, str]] = []
        for bid in backend_ids:
            for i in range(vnodes):
                pts.append((_h64(f"{bid}#{i}"), bid))
        pts.sort()
        self._points = [p for p, _ in pts]
        self._owners = [b for _, b in pts]

    def _start(self, key: str) -> int:
        idx = bisect.bisect_left(self._points, _h64(key))
        return idx if idx < len(self._points) else 0

    def primary(self, key: str, eligible: Callable[[str], bool]) -> Optional[str]:
        """key 顺时针命中的第一个满足 eligible 的后端；没有则 None。"""
        if not self._points:
            return None
        seen = set()
        idx = self._start(key)
        for _ in range(len(self._points)):
            owner = self._owners[idx]
            if owner not in seen:
                seen.add(owner)
                if eligible(owner):
                    return owner
            idx = (idx + 1) % len(self._points)
        return None

    def successor(self, key: str, after: str, eligible: Callable[[str], bool]) -> Optional[str]:
        """key 在环上除 after 之外、顺时针最近的满足 eligible 的后端。"""
        return self.primary(key, lambda b: b != after and eligible(b))


@dataclass
class SessionState:
    version: int
    payload: Dict[str, Any]
    checksum: str
    epoch: int


class Backend:
    """模拟后端：内存态 + 同步 WAL（journal）。WAL 是崩溃后唯一事实来源。"""

    def __init__(self, backend_id: str):
        self.id = backend_id
        self.healthy = True
        self.journal: Dict[str, SessionState] = {}   # 持久层（崩溃不丢）
        self._live: Dict[str, SessionState] = {}     # 内存态（崩溃即丢）

    # -- 生命周期 ----------------------------------------------------------
    def crash(self) -> None:
        self.healthy = False
        self._live = {}                              # 内存态丢失

    def boot(self) -> None:
        self._live = {sid: SessionState(s.version, dict(s.payload), s.checksum, s.epoch)
                      for sid, s in self.journal.items()}
        self.healthy = True

    # -- 状态操作（协调器专用） --------------------------------------------
    def has_state(self, sid: str) -> bool:
        return sid in self._live

    def get_state(self, sid: str) -> Optional[SessionState]:
        return self._live.get(sid)

    def freeze_state(self, sid: str) -> None:
        pass  # 真实实现里会暂停该会话的写；模拟中由协调器串行保证

    def export_state(self, sid: str) -> Optional[SessionState]:
        s = self._live.get(sid)
        if s is None:
            return None
        return SessionState(s.version, dict(s.payload), s.checksum, s.epoch)

    def import_state(self, sid: str, state: SessionState) -> None:
        if _checksum(state.payload) != state.checksum:
            raise StickyError(f"checksum mismatch importing {sid} into {self.id}")
        self._live[sid] = SessionState(state.version, dict(state.payload), state.checksum, state.epoch)
        self.journal[sid] = SessionState(state.version, dict(state.payload),
                                         state.checksum, state.epoch)  # 落 WAL

    def activate_state(self, sid: str) -> None:
        pass

    def discard_state(self, sid: str) -> None:
        self._live.pop(sid, None)
        self.journal.pop(sid, None)

    def write_state(self, sid: str, state: SessionState) -> None:
        self._live[sid] = state
        self.journal[sid] = SessionState(state.version, dict(state.payload),
                                         state.checksum, state.epoch)  # 同步 WAL

    def recover_state(self, sid: str) -> Optional[SessionState]:
        """崩溃恢复路径：从 WAL 重建（迁移前源端已刷盘，故不丢）。"""
        s = self.journal.get(sid)
        if s is None:
            return None
        return SessionState(s.version, dict(s.payload), s.checksum, s.epoch)


@dataclass
class Pin:
    backend: str
    status: str = PIN_ACTIVE
    epoch: int = 0            # fencing token，单调递增
    ring_view: Tuple[str, ...] = ()   # 绑定时的健康后端集合（审计用）


@dataclass
class MigrationRecord:
    session: str
    src: str
    dst: str
    state_version: int
    checksum_before: str
    checksum_after: str
    ok: bool
    lost: bool
    graceful: bool


@dataclass
class Event:
    seq: int
    op: str
    session: str
    backend: str
    detail: str = ""


@dataclass
class Selection:
    session: str
    backend: Optional[str]
    outcome: str              # ok_new | ok_pinned | ok_migrated | rejected | no_backend
    migrated_from: Optional[str] = None


class StickyCoordinator:
    """粘性协调器：维护 pin 表、哈希环、迁移与摘除/恢复。"""

    def __init__(self, backend_ids: List[str], policy: str = MIGRATE,
                 capacity: Optional[int] = None, trace: bool = True):
        assert policy in (MIGRATE, REJECT)
        self.policy = policy
        self.capacity = capacity          # None 表示不做负载上限
        self.backends: Dict[str, Backend] = {b: Backend(b) for b in backend_ids}
        self.pins: Dict[str, Pin] = {}
        self._loads: Dict[str, int] = {b: 0 for b in backend_ids}
        self.migrations: List[MigrationRecord] = []
        self.events: List[Event] = []
        self._trace = trace
        self._seq = 0
        self._ring_view: Tuple[str, ...] = tuple(sorted(backend_ids))
        self._ring = HashRing(sorted(backend_ids))
        self._rebuild()

    # -- 内部工具 ----------------------------------------------------------
    def _log(self, op: str, session: str, backend: str, detail: str = "") -> None:
        if not self._trace:
            return
        self._seq += 1
        self.events.append(Event(self._seq, op, session, backend, detail))

    def _rebuild(self) -> None:
        healthy = sorted(b.id for b in self.backends.values() if b.healthy)
        self._ring_view = tuple(healthy)
        self._ring = HashRing(healthy)

    def _eligible(self, respect_load: bool) -> Callable[[str], bool]:
        def ok(bid: str) -> bool:
            b = self.backends.get(bid)
            if b is None or not b.healthy:
                return False
            if respect_load and self.capacity is not None and self.load(bid) >= self.capacity:
                return False
            return True
        return ok

    # -- 查询 --------------------------------------------------------------
    def load(self, backend_id: str) -> int:
        return self._loads[backend_id]

    def loads(self) -> Dict[str, int]:
        return {b: self.load(b) for b in sorted(self.backends)}

    def pin_of(self, session: str) -> Optional[str]:
        p = self.pins.get(session)
        return p.backend if p and p.status == PIN_ACTIVE else None

    # -- 选择（核心） -------------------------------------------------------
    def bind(self, session: str) -> Selection:
        """选择/解析会话归属。同一输入序列下结果完全确定。"""
        pin = self.pins.get(session)

        if pin is not None and pin.status == PIN_REJECTED:
            # REJECT 策略：原后端恢复则归还，否则继续拒绝（绝不改投他处）
            src = self.backends[pin.backend]
            if src.healthy:
                pin.status = PIN_ACTIVE
                self._loads[pin.backend] += 1
                self._log("unreject", session, pin.backend)
                return Selection(session, pin.backend, "ok_pinned")
            self._log("reject", session, pin.backend, "backend still down")
            return Selection(session, None, "rejected")

        if pin is not None and self.backends[pin.backend].healthy:
            return Selection(session, pin.backend, "ok_pinned")

        if pin is not None:
            # pin 的后端已摘除：按策略处理
            if self.policy == REJECT:
                pin.status = PIN_REJECTED
                self._loads[pin.backend] -= 1
                self._log("reject", session, pin.backend, "backend down, policy=reject")
                return Selection(session, None, "rejected")
            target = self._ring.successor(session, pin.backend, self._eligible(True))
            if target is None:
                target = self._ring.successor(session, pin.backend, self._eligible(False))
            if target is None:
                self._log("no_backend", session, pin.backend)
                return Selection(session, None, "no_backend")
            self._migrate_locked(session, pin, target, graceful=False)
            return Selection(session, target, "ok_migrated", migrated_from=pin.backend)

        # 首次绑定：哈希环 + 健康 + 负载
        target = self._ring.primary(session, self._eligible(True))
        if target is None:
            target = self._ring.primary(session, self._eligible(False))
        if target is None:
            self._log("no_backend", session, "-")
            return Selection(session, None, "no_backend")
        self.pins[session] = Pin(backend=target, epoch=1, ring_view=self._ring_view)
        self._loads[target] += 1
        if not self.backends[target].has_state(session):
            self.backends[target].write_state(
                session, SessionState(0, {}, _checksum({}), 1))
        self._log("bind", session, target)
        return Selection(session, target, "ok_new")

    # -- 迁移（两阶段，不丢状态） -------------------------------------------
    def _migrate_locked(self, session: str, pin: Pin, dst: str, graceful: bool) -> MigrationRecord:
        src_id = pin.backend
        src = self.backends[src_id]
        dstb = self.backends[dst]
        assert dstb.healthy

        src.freeze_state(session)                      # 1. 冻结源端写
        snap = src.export_state(session) if graceful else src.recover_state(session)
        lost = snap is None
        if lost:                                       # 源端彻底无状态：以空状态继续，不丢会话本身
            snap = SessionState(0, {}, _checksum({}), pin.epoch)
        before = snap.checksum

        new_epoch = pin.epoch + 1                      # 2. fencing token +1
        staged = SessionState(snap.version, dict(snap.payload), snap.checksum, new_epoch)
        dstb.import_state(session, staged)             # 3. 拷贝到目标（校验和验证）
        after = _checksum(staged.payload)
        ok = (not lost) and before == after and dstb.get_state(session).checksum == before

        dstb.activate_state(session)                   # 4. 目标激活
        pin.backend = dst                              # 5. 原子切换 pin
        pin.epoch = new_epoch
        pin.status = PIN_ACTIVE
        self._loads[src_id] -= 1
        self._loads[dst] += 1
        if graceful:
            src.discard_state(session)                 # 6. 清理源端（崩溃路径源端内存已丢）

        rec = MigrationRecord(session, src_id, dst, staged.version, before, after,
                              ok, lost, graceful)
        self.migrations.append(rec)
        self._log("migrate", session, dst, f"from={src_id} ok={ok} lost={lost}")
        return rec

    # -- 会话读写（带 fencing） ---------------------------------------------
    def read_state(self, session: str) -> Dict[str, Any]:
        pin = self.pins[session]
        st = self.backends[pin.backend].get_state(session)
        return dict(st.payload) if st else {}

    def write_session(self, session: str, mutate: Callable[[Dict[str, Any], int], None]) -> None:
        """在当前归属后端上修改会话状态；旧后端持过期 epoch 写入会被拒。"""
        pin = self.pins[session]
        if pin.status != PIN_ACTIVE:
            raise SessionRejectedError(session)
        backend = self.backends[pin.backend]
        if not backend.healthy:
            raise NoBackendError(pin.backend)
        cur = backend.get_state(session)
        payload = dict(cur.payload) if cur else {}
        version = (cur.version if cur else 0) + 1
        mutate(payload, version)
        backend.write_state(session, SessionState(version, payload,
                                                  _checksum(payload), pin.epoch))

    def write_on_backend(self, backend_id: str, session: str,
                         payload: Dict[str, Any], epoch: int) -> None:
        """模拟后端直接收到的写入；fencing token 过期即拒（防脑裂写）。"""
        pin = self.pins.get(session)
        if pin is None or pin.backend != backend_id or epoch < pin.epoch:
            raise StaleEpochError(f"{backend_id} epoch={epoch}")
        self.backends[backend_id].write_state(
            session, SessionState(0, payload, _checksum(payload), epoch))

    # -- 后端摘除 / 恢复 ------------------------------------------------------
    def remove_backend(self, backend_id: str, graceful: bool = True) -> Dict[str, int]:
        """摘除后端。MIGRATE：受影响会话沿环确定性迁移；REJECT：全部拒绝。"""
        b = self.backends[backend_id]
        if graceful:
            b.healthy = False                          # 排空模式：内存态仍在，可拷贝
        else:
            b.crash()                                  # 崩溃：内存态丢失，靠 WAL
        self._rebuild()

        stats = {"migrated": 0, "rejected": 0, "failed": 0, "lost": 0}
        for session in sorted(self.pins):              # 排序遍历 → 迁移顺序确定
            pin = self.pins[session]
            if pin.backend != backend_id or pin.status != PIN_ACTIVE:
                continue
            if self.policy == REJECT:
                pin.status = PIN_REJECTED
                self._loads[backend_id] -= 1
                stats["rejected"] += 1
                self._log("reject", session, backend_id, "policy=reject")
                continue
            target = self._ring.successor(session, backend_id, self._eligible(True))
            if target is None:
                target = self._ring.successor(session, backend_id, self._eligible(False))
            if target is None:
                stats["failed"] += 1
                self._log("no_backend", session, backend_id)
                continue
            rec = self._migrate_locked(session, pin, target, graceful)
            stats["migrated"] += 1
            stats["lost"] += 1 if rec.lost else 0
        return stats

    def recover_backend(self, backend_id: str) -> None:
        """恢复后端：从 WAL 重建内存态。已迁走的会话归属不变（pin 优先）。"""
        b = self.backends[backend_id]
        b.boot()
        self._rebuild()
        self._log("recover", "-", backend_id)

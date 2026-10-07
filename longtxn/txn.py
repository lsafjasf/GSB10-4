"""事务管理器：水位推进、长事务识别、超长事务处置。

水位规则（GC 水位，commit_seq 域）：
  watermark = min(active.start_seq)   存在活跃事务
            = commit_seq              无活跃事务
  单调不减；活跃事务释放快照（commit/abort/degrade/force_abort）后
  在下一次 watermark() 计算时推进。

识别规则（时间域，时钟由外部注入）：
  age = clock.now() - start_time
  age > long_threshold            -> LONG（只报告，不动作）
  age > max_duration              -> 处置：
      只读事务 -> degrade     （释放快照钉，此后读到最新已提交数据）
      有写事务 -> force_abort （丢弃缓冲写，事务终止）
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .clock import ManualClock
from .persistence import StateLog, replay
from .store import Version, VersionStore

ACTIVE = "active"
COMMITTED = "committed"
ABORTED = "aborted"
DEGRADED = "degraded"
FORCE_ABORTED = "force_aborted"


@dataclass
class Transaction:
    txn_id: int
    start_seq: int
    start_time: float
    state: str = ACTIVE
    writes: List[Tuple[str, Any]] = field(default_factory=list)
    read_seq: Optional[int] = None  # None -> 用 start_seq；降级后读最新

    @property
    def read_only(self) -> bool:
        return not self.writes


@dataclass
class DisposalEvent:
    txn_id: int
    age: float
    action: str  # "degrade" | "force_abort"
    watermark_before: int
    watermark_after: int


class TransactionManager:
    def __init__(self, store: VersionStore, clock: ManualClock,
                 long_threshold: float = 30.0,
                 max_duration: float = 120.0,
                 state_log: Optional[StateLog] = None):
        if not (0 < long_threshold < max_duration):
            raise ValueError("need 0 < long_threshold < max_duration")
        self.store = store
        self.clock = clock
        self.long_threshold = long_threshold
        self.max_duration = max_duration
        self.log = state_log
        self.commit_seq = 0
        self._next_txn_id = 1
        self._active: Dict[int, Transaction] = {}
        self._detached: Dict[int, Transaction] = {}  # 降级事务：不再钉水位，仍可读
        self.disposals: List[DisposalEvent] = []
        self.recovered_orphans: List[int] = []

    # ---- 事务生命周期 -------------------------------------------------

    def begin(self) -> int:
        txn_id = self._next_txn_id
        self._next_txn_id += 1
        txn = Transaction(txn_id=txn_id, start_seq=self.commit_seq,
                          start_time=self.clock.now())
        self._active[txn_id] = txn
        if self.log:
            self.log.log_begin(txn_id, txn.start_seq, txn.start_time)
        return txn_id

    def write(self, txn_id: int, key: str, value: Any) -> None:
        txn = self._require_active(txn_id)
        txn.writes.append((key, value))

    def read(self, txn_id: int, key: str) -> Any:
        txn = self._active.get(txn_id)
        if txn is None:
            txn = self._detached.get(txn_id)
        if txn is None:
            raise KeyError(f"txn {txn_id} not active")
        if txn.state == DEGRADED:
            seq = self.commit_seq  # 降级后读最新已提交数据
        else:
            seq = txn.start_seq
        v = self.store.visible_at(key, seq)
        return None if v is None else v.value

    def commit(self, txn_id: int) -> int:
        txn = self._require_active(txn_id)
        self.commit_seq += 1
        seq = self.commit_seq
        for key, value in txn.writes:
            self.store.add(Version(key=key, commit_seq=seq,
                                   txn_id=txn_id, value=value))
            if self.log:
                self.log.log_version(txn_id, seq, key, value)
        txn.state = COMMITTED
        del self._active[txn_id]
        if self.log:
            self.log.log_terminal(txn_id, "commit", commit_seq=seq)
        return seq

    def abort(self, txn_id: int) -> None:
        txn = self._require_active(txn_id)
        txn.state = ABORTED
        txn.writes.clear()
        del self._active[txn_id]
        if self.log:
            self.log.log_terminal(txn_id, "abort")

    # ---- 水位 ----------------------------------------------------------

    def watermark(self) -> int:
        if not self._active:
            return self.commit_seq
        return min(t.start_seq for t in self._active.values())

    # ---- 长事务识别 ----------------------------------------------------

    def identify_long_txns(self) -> List[Dict[str, Any]]:
        now = self.clock.now()
        result = []
        for txn in self._active.values():
            age = now - txn.start_time
            if age > self.long_threshold:
                result.append({
                    "txn_id": txn.txn_id,
                    "age": age,
                    "read_only": txn.read_only,
                    "over_max": age > self.max_duration,
                })
        return sorted(result, key=lambda r: -r["age"])

    # ---- 超长事务处置 --------------------------------------------------

    def enforce(self) -> List[DisposalEvent]:
        now = self.clock.now()
        events: List[DisposalEvent] = []
        for txn_id, txn in list(self._active.items()):
            age = now - txn.start_time
            if age <= self.max_duration:
                continue
            wm_before = self.watermark()
            if txn.read_only:
                txn.state = DEGRADED
                txn.read_seq = None
                del self._active[txn_id]
                self._detached[txn_id] = txn
                action = "degrade"
            else:
                txn.state = FORCE_ABORTED
                txn.writes.clear()
                del self._active[txn_id]
                action = "force_abort"
            if self.log:
                self.log.log_terminal(txn_id, action)
            ev = DisposalEvent(txn_id=txn_id, age=age, action=action,
                               watermark_before=wm_before,
                               watermark_after=self.watermark())
            self.disposals.append(ev)
            events.append(ev)
        return events

    # ---- 崩溃恢复 ------------------------------------------------------

    @classmethod
    def recover(cls, store: VersionStore, clock: ManualClock,
                log_path: str, **kwargs) -> "TransactionManager":
        mgr = cls(store, clock, state_log=StateLog(log_path), **kwargs)
        active, commit_seq, versions, orphaned = replay(log_path)
        for rec in versions:
            store.add(Version(key=rec["key"], commit_seq=rec["commit_seq"],
                              txn_id=rec["txn_id"], value=rec["value"]))
        mgr.commit_seq = commit_seq
        mgr.recovered_orphans = sorted(orphaned)
        max_id = 0
        for txn_id, meta in active.items():
            mgr._active[txn_id] = Transaction(
                txn_id=txn_id, start_seq=meta["start_seq"],
                start_time=meta["start_time"])
            max_id = max(max_id, txn_id)
        for txn_id in orphaned:
            max_id = max(max_id, txn_id)
            if mgr.log:
                mgr.log.log_terminal(txn_id, "force_abort")
        mgr._next_txn_id = max_id + 1
        return mgr

    # ---- 内部 ----------------------------------------------------------

    def _require_active(self, txn_id: int) -> Transaction:
        txn = self._active.get(txn_id)
        if txn is None:
            raise KeyError(f"txn {txn_id} not active")
        return txn

    def active_txns(self) -> Dict[int, Transaction]:
        return dict(self._active)

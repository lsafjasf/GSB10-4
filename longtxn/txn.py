"""Transaction registry with long-transaction identification and disposal."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple


# Default policy thresholds, in seconds of injected time.
LONG_THRESHOLD_S = 30.0   # active age >= 30s  -> identified as "long transaction"
KILL_THRESHOLD_S = 120.0  # active age >= 120s -> force-aborted (time limit)


class TxnState(str, Enum):
    ACTIVE = "active"
    COMMITTED = "committed"
    ABORTED = "aborted"
    FORCED_ABORT = "forced_abort"  # killed by the long-txn disposal policy


@dataclass
class Txn:
    txn_id: int
    start_ts: float
    state: TxnState = TxnState.ACTIVE
    identified_long_at: Optional[float] = None
    killed_at: Optional[float] = None
    writes: int = 0

    @property
    def is_active(self) -> bool:
        return self.state == TxnState.ACTIVE


@dataclass
class ScanResult:
    now: float
    identified: List[Txn] = field(default_factory=list)
    killed: List[Txn] = field(default_factory=list)
    active: List[Txn] = field(default_factory=list)

    def summary(self) -> str:
        ids = lambda xs: sorted(t.txn_id for t in xs)
        return (
            f"t={self.now:.3f}s active={ids(self.active)} "
            f"identified_long={ids(self.identified)} "
            f"force_aborted={ids(self.killed)}"
        )


class TxnManager:
    """Holds all transactions and computes the GC water level (horizon).

    Water level rule:
        water_level = min(start_ts of ACTIVE transactions)
    If no active transaction exists:
        water_level = clock.now()
    A reader snapshot at `water_level` must see exactly the data the oldest
    active transaction can see, so GC is forbidden to cross this line.
    """

    def __init__(
        self,
        clock: Callable[[], float],
        long_threshold_s: float = LONG_THRESHOLD_S,
        kill_threshold_s: float = KILL_THRESHOLD_S,
    ):
        self._clock = clock
        self.long_threshold_s = float(long_threshold_s)
        self.kill_threshold_s = float(kill_threshold_s)
        if self.kill_threshold_s < self.long_threshold_s:
            raise ValueError("kill_threshold_s must be >= long_threshold_s")
        self._txns: Dict[int, Txn] = {}
        self._next_id = 1

    def begin(self) -> Txn:
        t = Txn(txn_id=self._next_id, start_ts=self._clock())
        self._next_id += 1
        self._txns[t.txn_id] = t
        return t

    def commit(self, txn: Txn) -> Txn:
        if txn.state != TxnState.ACTIVE:
            raise RuntimeError(f"txn {txn.txn_id} not active")
        txn.state = TxnState.COMMITTED
        return txn

    def abort(self, txn: Txn) -> Txn:
        if txn.state != TxnState.ACTIVE:
            raise RuntimeError(f"txn {txn.txn_id} not active")
        txn.state = TxnState.ABORTED
        return txn

    def get(self, txn_id: int) -> Txn:
        return self._txns[txn_id]

    def active_txns(self) -> List[Txn]:
        return [t for t in self._txns.values() if t.is_active]

    def oldest_active(self) -> Optional[Txn]:
        actives = self.active_txns()
        return min(actives, key=lambda t: t.start_ts) if actives else None

    def water_level(self) -> float:
        oldest = self.oldest_active()
        if oldest is None:
            return self._clock()
        return oldest.start_ts

    def scan(self, now: Optional[float] = None) -> ScanResult:
        """Identify long transactions and dispose of over-long ones.

        Identification: age >= long_threshold_s (one-shot flag).
        Disposal:       age >= kill_threshold_s -> FORCE_ABORT (hard time limit).
        """
        now = self._clock() if now is None else now
        result = ScanResult(now=now)
        for t in self.active_txns():
            age = now - t.start_ts
            if age >= self.long_threshold_s and t.identified_long_at is None:
                t.identified_long_at = now
                result.identified.append(t)
            if age >= self.kill_threshold_s:
                t.state = TxnState.FORCED_ABORT
                t.killed_at = now
                result.killed.append(t)
            elif t.is_active:
                result.active.append(t)
        return result

    def force_abort(self, txn: Txn, now: Optional[float] = None) -> Txn:
        """Manual disposal (e.g. operator action)."""
        now = self._clock() if now is None else now
        txn.state = TxnState.FORCED_ABORT
        txn.killed_at = now
        return txn

    # ---------------- persistence / crash recovery ----------------
    def snapshot(self) -> dict:
        return {
            "next_id": self._next_id,
            "long_threshold_s": self.long_threshold_s,
            "kill_threshold_s": self.kill_threshold_s,
            "txns": [
                {
                    "txn_id": t.txn_id,
                    "start_ts": t.start_ts,
                    "state": t.state.value,
                    "identified_long_at": t.identified_long_at,
                    "killed_at": t.killed_at,
                    "writes": t.writes,
                }
                for t in self._txns.values()
            ],
        }

    @classmethod
    def restore(cls, payload: dict, clock: Callable[[], float]) -> "TxnManager":
        m = cls(
            clock,
            long_threshold_s=payload["long_threshold_s"],
            kill_threshold_s=payload["kill_threshold_s"],
        )
        m._next_id = payload["next_id"]
        for row in payload["txns"]:
            m._txns[row["txn_id"]] = Txn(
                txn_id=row["txn_id"],
                start_ts=row["start_ts"],
                state=TxnState(row["state"]),
                identified_long_at=row["identified_long_at"],
                killed_at=row["killed_at"],
                writes=row.get("writes", 0),
            )
        return m

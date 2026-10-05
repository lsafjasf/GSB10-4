"""Minimal append-only MVCC key/value store.

Each key holds a version chain ordered ascending by commit timestamp.
read(key, ts) returns the newest version with commit_ts <= ts (snapshot read).
gc(water_level) must never remove a version visible at water_level.
"""
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Version:
    value: Any
    commit_ts: float


class MVCCStore:
    def __init__(self):
        self._data: Dict[Any, List[Version]] = {}

    def put(self, key: Any, value: Any, commit_ts: float) -> None:
        chain = self._data.setdefault(key, [])
        if chain and commit_ts <= chain[-1].commit_ts:
            raise ValueError("commit timestamps must be strictly increasing per key")
        chain.append(Version(value, commit_ts))

    def read(self, key: Any, ts: float) -> Any:
        chain = self._data.get(key)
        if not chain:
            return None
        idx = bisect_right(chain, ts, key=lambda v: v.commit_ts)
        if idx == 0:
            return None
        return chain[idx - 1].value

    def gc(self, water_level: float) -> int:
        """Drop versions invisible to every reader at ts >= water_level.

        Rule: for each key keep
          * every version with commit_ts > water_level, and
          * exactly one newest version with commit_ts <= water_level
            (the version visible AT the water level).
        Returns number of versions removed. Idempotent.
        """
        removed = 0
        for key, chain in self._data.items():
            cutoff = bisect_right(chain, water_level, key=lambda v: v.commit_ts)
            if cutoff >= 2:
                del chain[: cutoff - 1]
                removed += cutoff - 1
        return removed

    def version_count(self) -> int:
        return sum(len(chain) for chain in self._data.values())

    def key_count(self) -> int:
        return len(self._data)

    # ---- serialization used by crash/recovery tests (small datasets) ----
    def to_dict(self) -> Dict[str, List[Tuple[float, Any]]]:
        return {
            repr(k): [(v.commit_ts, v.value) for v in chain]
            for k, chain in self._data.items()
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, List[Tuple[float, Any]]]) -> "MVCCStore":
        store = cls()
        for k, versions in payload.items():
            store._data[k] = [Version(value=val, commit_ts=ts) for ts, val in versions]
        return store

    def visible_dump(self, ts: float, keys: Optional[List[Any]] = None) -> Dict[Any, Any]:
        """Snapshot of what a reader at `ts` sees; used as comparison data."""
        keys = keys if keys is not None else list(self._data.keys())
        return {k: self.read(k, ts) for k in keys}

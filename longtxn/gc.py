"""GC coordinator: water-level driven cleanup with crash-recovery journal.

Durable layout (a directory):
    state.json   -- last committed TxnManager snapshot + completed water level
    journal.json -- {"phase": "started"|"done", "water_level": ...}

GC protocol:
    1. wl = manager.water_level()                 # never crosses active txns
    2. state.json = snapshot(manager, gc_pending, wl)  # durable active set
       (a force-abort decision MUST be durable before cleanup starts)
    3. journal = {"phase":"started", wl} ; fsync
    4. store.gc(wl)                               # idempotent
    5. journal = {"phase":"done", wl} ; fsync
    6. state.json = snapshot(manager, gc_done, wl)

Crash recovery:
    * journal missing / "done" -> nothing to do.
    * journal "started" -> cleanup was interrupted; replay store.gc(wl)
      (idempotent) against the durable store, then finish protocol.
"""
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Optional


@dataclass
class GCStats:
    water_level: float
    removed_versions: int
    elapsed_ms: float

    def __str__(self) -> str:
        return (
            f"water_level={self.water_level:.3f}s "
            f"removed={self.removed_versions} elapsed={self.elapsed_ms:.3f}ms"
        )


def _atomic_write(path: str, obj: dict) -> None:
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.fsync(os.open(d, os.O_RDONLY))
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class GCCoordinator:
    def __init__(self, store, manager, state_dir: Optional[str] = None):
        self.store = store
        self.manager = manager
        self.state_dir = state_dir
        self.state_path = os.path.join(state_dir, "state.json") if state_dir else None
        self.journal_path = os.path.join(state_dir, "journal.json") if state_dir else None
        self.last_water_level: Optional[float] = None
        self._elapsed = _perf_ns
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)

    def _write_journal(self, phase: str, water_level: float) -> None:
        if self.journal_path:
            _atomic_write(self.journal_path, {"phase": phase, "water_level": water_level})

    def _persist_state(self, water_level: float, phase: str) -> None:
        if self.state_path:
            payload = self.manager.snapshot()
            payload["gc_phase"] = phase
            payload["target_water_level"] = water_level
            if phase == "gc_done":
                payload["completed_water_level"] = water_level
            _atomic_write(self.state_path, payload)

    def _time_ms(self, ns: int) -> float:
        return ns / 1_000_000.0

    def run_gc(self) -> GCStats:
        wl = self.manager.water_level()
        self._persist_state(wl, "gc_pending")
        self._write_journal("started", wl)
        t0 = self._elapsed()
        removed = self.store.gc(wl)
        elapsed = self._time_ms(self._elapsed() - t0)
        self._write_journal("done", wl)
        self.last_water_level = wl
        self._persist_state(wl, "gc_done")
        return GCStats(water_level=wl, removed_versions=removed, elapsed_ms=elapsed)

    # ---------------- crash recovery ----------------
    @staticmethod
    def read_journal(state_dir: str) -> Optional[dict]:
        path = os.path.join(state_dir, "journal.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def read_state(state_dir: str) -> Optional[dict]:
        path = os.path.join(state_dir, "state.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def recover(self) -> Optional[GCStats]:
        """Replay an interrupted cleanup. Returns None if nothing to replay."""
        if self.journal_path is None or not os.path.exists(self.journal_path):
            return None
        with open(self.journal_path, encoding="utf-8") as f:
            journal = json.load(f)
        if journal.get("phase") != "started":
            return None
        wl = float(journal["water_level"])
        # Water level from the journal is authoritative: GC must not advance
        # past it during replay, even if the active set is unknown/stale.
        current = self.manager.water_level()
        replay_wl = min(wl, current) if current is not None else wl
        t0 = self._elapsed()
        removed = self.store.gc(replay_wl)
        elapsed = self._time_ms(self._elapsed() - t0)
        self._write_journal("done", replay_wl)
        self.last_water_level = replay_wl
        self._persist_state(replay_wl, "gc_done")
        return GCStats(water_level=replay_wl, removed_versions=removed, elapsed_ms=elapsed)


try:
    from time import perf_counter_ns as _perf_ns  # py3.7+
except ImportError:  # pragma: no cover
    import time

    def _perf_ns() -> int:
        return int(time.time() * 1e9)

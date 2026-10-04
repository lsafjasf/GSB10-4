"""Session stickiness + backend selection.

Stickiness rules (see README for the full description):

1. Session identity: every request carries a session id ``sid``.
2. Pinning: once a session is placed, the router keeps an explicit pin
   ``sid -> backend``; a pinned session always returns its owner while the
   owner is healthy (load never preempts an existing pin).
3. Placement of a new (or migrating) session is deterministic:
   among *healthy* backends, pick the minimum key
   ``(ring_rank(sid, backend), load(backend), backend_id)``.
   ``ring_rank`` is the clockwise distance on a fixed consistent-hash ring
   (virtual nodes), ``load`` is the number of live pinned sessions on the
   backend. The ``(load, backend_id)`` tie-breakers make the result a pure
   function of ``(sid, healthy-set, load-snapshot)`` -- no randomness, no
   wall clock.
4. Drift: when the pinned backend is unhealthy, either migrate (rule 3 picks
   the new owner and state is transferred with a checksummed snapshot) or
   reject, depending on the configured policy.
5. Recovery only flips the health flag; pins are never rewritten, so a
   recovered backend does not steal sessions that migrated away.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from bisect import bisect_left
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_VNODES = 160
DEFAULT_LOAD_SPREAD = 0.0

_RING_MOD = 1 << 128


def _ring_hash(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest(), "big")


def _canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class StickyError(Exception):
    """Base class for router errors."""


class NoHealthyBackend(StickyError):
    """Raised when a session with no pin needs a backend and none is healthy."""


class SessionRejected(StickyError):
    """Raised under the ``reject`` policy when the pinned backend is down."""


class StateConflict(StickyError):
    """Raised on a CAS conflict or a corrupted session snapshot."""


@dataclass(frozen=True)
class Snapshot:
    """Immutable, checksummed session-state snapshot used for migration.

    The checksum chains to the previous one (``prev_checksum``), so any
    dropped/corrupted update is detected on the destination backend.
    """

    session_id: str
    revision: int
    data: Any
    checksum: str
    prev_checksum: str = ""

    @staticmethod
    def capture(session_id: str, revision: int, data: Any, prev_checksum: str) -> "Snapshot":
        body = f"{session_id}\n{revision}\n{_canonical(data).decode('utf-8')}\n{prev_checksum}"
        checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return Snapshot(session_id, revision, data, checksum, prev_checksum)

    def verify(self) -> None:
        expected = Snapshot.capture(
            self.session_id, self.revision, self.data, self.prev_checksum
        ).checksum
        if not hmac.compare_digest(expected, self.checksum):
            raise StateConflict(f"checksum mismatch for session {self.session_id!r}")


class Backend:
    """A backend with its local session store.

    State is kept in-process for the self-test; the save/load/verify surface
    is the same a networked state-transfer implementation would use.
    """

    def __init__(self, backend_id: str):
        self.backend_id = backend_id
        self.states: Dict[str, Snapshot] = {}

    def save(self, snapshot: Snapshot) -> None:
        snapshot.verify()
        self.states[snapshot.session_id] = snapshot

    def load(self, snapshot: Snapshot) -> None:
        snapshot.verify()
        self.states[snapshot.session_id] = snapshot

    def get(self, session_id: str) -> Optional[Snapshot]:
        snap = self.states.get(session_id)
        if snap is not None:
            snap.verify()
        return snap

    def discard(self, session_id: str) -> None:
        self.states.pop(session_id, None)


@dataclass(frozen=True)
class MigrationRecord:
    session_id: str
    src: str
    dst: str
    revision: int
    checksum_before: str
    checksum_after: str
    state_lost: bool


class StickyRouter:
    def __init__(
        self,
        backend_ids: List[str],
        vnodes: int = DEFAULT_VNODES,
        policy: str = "migrate",
        load_spread: float = DEFAULT_LOAD_SPREAD,
    ):
        if policy not in ("migrate", "reject"):
            raise ValueError("policy must be 'migrate' or 'reject'")
        if not backend_ids:
            raise ValueError("at least one backend required")
        if len(set(backend_ids)) != len(backend_ids):
            raise ValueError("backend ids must be unique")
        self.policy = policy
        self.load_spread = load_spread
        self.backends: Dict[str, Backend] = {b: Backend(b) for b in backend_ids}
        self.healthy: Dict[str, bool] = {b: True for b in backend_ids}
        self.pins: Dict[str, str] = {}
        self.load: Dict[str, int] = {b: 0 for b in backend_ids}
        self.history: List[MigrationRecord] = []
        self._lock = threading.RLock()
        self._build_ring(sorted(backend_ids), vnodes)

    def _build_ring(self, backend_ids: List[str], vnodes: int) -> None:
        points: List[Tuple[int, str, int]] = []
        for backend_id in backend_ids:
            for i in range(vnodes):
                points.append((_ring_hash(f"{backend_id}#{i}"), backend_id, i))
        points.sort(key=lambda p: (p[0], p[1], p[2]))
        self._ring = points
        self._ring_hashes = [p[0] for p in points]

    def _iter_healthy_clockwise(self, session_id: str):
        """Yield ``(rank, backend_id)`` for healthy backends in clockwise ring
        order from the session hash. Lazy: callers stop after the first hit
        in the common case."""
        key = _ring_hash(session_id)
        start = bisect_left(self._ring_hashes, key)
        n = len(self._ring)
        seen = set()
        rank = 0
        for step in range(n):
            _, backend_id, _ = self._ring[(start + step) % n]
            if backend_id in seen:
                continue
            seen.add(backend_id)
            if self.healthy[backend_id]:
                yield rank, backend_id
                rank += 1

    def _placement(self, session_id: str) -> Optional[str]:
        """Deterministic new-owner choice: min (rank, load, backend_id).

        Returns ``None`` when no backend is healthy. Stops early once the
        ring-preferred backend is no more than ``load_spread`` sessions above
        the current mean load, so the common case costs O(vnodes until first
        hit); the worst case is a full ring scan with an identical result.
        """
        mean_load = sum(self.load.values()) / len(self.backends)
        best: Optional[Tuple[int, int, str]] = None
        for rank, backend_id in self._iter_healthy_clockwise(session_id):
            key = (rank, self.load[backend_id], backend_id)
            if best is None or key < best:
                best = key
            if self.load[backend_id] <= mean_load + self.load_spread:
                return backend_id
        return best[2] if best is not None else None

    def owner_of(self, session_id: str) -> Optional[str]:
        with self._lock:
            return self.pins.get(session_id)

    def select(self, session_id: str) -> str:
        """Return the backend that should serve ``session_id``.

        Applies pinning, health, load-aware placement and the configured
        drift policy. Raises ``NoHealthyBackend`` or ``SessionRejected``.
        """
        with self._lock:
            owner = self.pins.get(session_id)
            if owner is not None and self.healthy[owner]:
                return owner
            if owner is not None and self.policy == "reject":
                raise SessionRejected(
                    f"session {session_id!r} pinned to dead backend {owner!r}"
                )
            target = self._placement(session_id)
            if target is None:
                if owner is None:
                    raise NoHealthyBackend(f"no healthy backend for session {session_id!r}")
                raise NoHealthyBackend(
                    f"cannot migrate session {session_id!r}: all backends are down"
                )
            if owner is None:
                self.pins[session_id] = target
                self.load[target] += 1
                return target
            return self._commit_migration(session_id, owner, target)

    def _commit_migration(self, session_id: str, src: str, dst: str) -> str:
        snap = self.backends[src].states.get(session_id)
        before = snap.checksum if snap is not None else ""
        revision = snap.revision if snap is not None else -1
        lost = False
        if snap is not None:
            snap.verify()
            self.backends[dst].load(snap)
            after = self.backends[dst].get(session_id).checksum
        else:
            # Session had a pin but no state yet: migration is a pure
            # re-pin, nothing can be lost.
            after = ""
        self.pins[session_id] = dst
        self.load[src] -= 1
        self.load[dst] += 1
        self.history.append(
            MigrationRecord(session_id, src, dst, revision, before, after, lost)
        )
        return dst

    def migrate(self, session_id: str) -> str:
        """Force migration of a pinned session (used while draining a backend)."""
        with self._lock:
            owner = self.pins.get(session_id)
            if owner is None:
                raise KeyError(f"session {session_id!r} is not pinned")
            target = self._placement(session_id)
            if target is None:
                raise NoHealthyBackend(f"cannot migrate {session_id!r}: no healthy backend")
            if target == owner:
                return owner
            return self._commit_migration(session_id, owner, target)

    def drain_backend(self, backend_id: str) -> List[str]:
        """Mark backend unhealthy and migrate all its pins (sorted, deterministic)."""
        with self._lock:
            if backend_id not in self.backends:
                raise KeyError(f"unknown backend {backend_id!r}")
            self.healthy[backend_id] = False
            moved: List[str] = []
            for sid in sorted(s for s, b in self.pins.items() if b == backend_id):
                moved.append(self.select(sid))
            return moved

    def set_health(self, backend_id: str, healthy: bool) -> None:
        with self._lock:
            if backend_id not in self.backends:
                raise KeyError(f"unknown backend {backend_id!r}")
            self.healthy[backend_id] = healthy

    def put_state(self, session_id: str, data: Any, expected_revision: Optional[int] = None) -> Snapshot:
        """CAS state write on the session's current owner backend.

        Snapshots form a hash chain: each revision's checksum includes the
        previous revision's checksum, so the destination of a migration can
        detect dropped or corrupted updates.
        """
        with self._lock:
            owner = self.pins.get(session_id)
            if owner is None:
                raise KeyError(f"session {session_id!r} is not pinned; call select() first")
            backend = self.backends[owner]
            old = backend.states.get(session_id)
            if expected_revision is not None and old is not None and old.revision != expected_revision:
                raise StateConflict(
                    f"revision conflict for {session_id!r}: "
                    f"have {old.revision}, expected {expected_revision}"
                )
            revision = 0 if old is None else old.revision + 1
            prev_checksum = "" if old is None else old.checksum
            snap = Snapshot.capture(session_id, revision, data, prev_checksum)
            backend.save(snap)
            return snap

    def get_state(self, session_id: str) -> Optional[Snapshot]:
        with self._lock:
            owner = self.pins.get(session_id)
            if owner is None:
                return None
            return self.backends[owner].get(session_id)

    def snapshot_view(self) -> Dict[str, Any]:
        """Serializable view used by the before/after diff checks."""
        with self._lock:
            view: Dict[str, Any] = {}
            for sid, owner in self.pins.items():
                snap = self.backends[owner].states.get(sid)
                view[sid] = {
                    "owner": owner,
                    "revision": snap.revision if snap is not None else None,
                    "data": snap.data if snap is not None else None,
                    "checksum": snap.checksum if snap is not None else None,
                }
            return view

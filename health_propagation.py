"""Health status propagation with flapping suppression (stdlib only).

Design notes
------------
* Each node advertises (seq, health, ts). `seq` is monotonic per node.
* Every node's effective state is a *pure function* of its retained update
  set and the evaluation time. Retention depends only on `max_seq`, never on
  arrival order, so any interleaving of the same batch of updates converges
  to byte-identical state (convergence by construction).
* Flapping suppression uses an explicit sliding window + threshold.
* Removal and recovery use symmetric consecutive-report counts; recovery
  additionally requires a stability interval, so it is never more aggressive
  than removal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple


class Health(Enum):
    UP = 0
    DOWN = 1  # DOWN sorts higher: deterministic "DOWN wins" conflict merge


class Effective(Enum):
    UP = "UP"
    DOWN = "DOWN"
    DAMPED = "DAMPED"    # flapping suppressed; `held` carries the frozen value
    REMOVED = "REMOVED"  # evicted from the upstream rotation


@dataclass(frozen=True)
class Update:
    node_id: str
    seq: int
    health: Health
    ts: float


@dataclass(frozen=True)
class Params:
    flap_window: float = 10.0     # sliding window (s) for counting state flips
    flap_threshold: int = 4       # >= this many flips inside window -> suppress
    damp_quiet: float = 10.0      # suppression lifts only after this quiet time
    remove_after: int = 3         # consecutive DOWN reports -> REMOVED
    recover_after: int = 3        # consecutive UP reports -> eligible to recover
    recover_stable: float = 10.0  # ... and newest UP >= this long after last DOWN
    history: int = 64             # retained updates per node (bounded memory)

    def __post_init__(self) -> None:
        # Recovery must not be more aggressive than removal.
        assert self.recover_after >= self.remove_after, "recovery too eager"
        assert self.damp_quiet >= self.flap_window, "damping release too eager"
        assert self.history >= self.remove_after + self.recover_after + 1


@dataclass(frozen=True)
class View:
    effective: Effective
    held: Health          # value peers should use (frozen while DAMPED)
    raw: Health           # newest advertised value, unsuppressed
    flips_in_window: int


class NodeRecord:
    __slots__ = ("updates", "max_seq")

    def __init__(self) -> None:
        self.updates: Dict[int, Tuple[Health, float]] = {}
        self.max_seq: int = -1


class Cluster:
    """Merge endpoint for health updates gossiped between peers."""

    def __init__(self, params: Params = Params()) -> None:
        self.params = params
        self.nodes: Dict[str, NodeRecord] = {}
        self.accepted = 0
        self.duplicates = 0
        self.stale = 0
        self.conflicts = 0

    # ------------------------------------------------------------------ merge
    def apply(self, u: Update) -> str:
        """Merge one update. Returns accepted/duplicate/stale/conflict."""
        rec = self.nodes.get(u.node_id)
        if rec is None:
            rec = NodeRecord()
            self.nodes[u.node_id] = rec

        existing = rec.updates.get(u.seq)
        if existing is not None:
            if existing[0] is u.health:
                self.duplicates += 1
                return "duplicate"
            # Same seq, different value: merge deterministically, DOWN wins,
            # keep the earlier timestamp. Order-independent by construction.
            merged = Health(max(existing[0].value, u.health.value))
            rec.updates[u.seq] = (merged, min(existing[1], u.ts))
            self.conflicts += 1
            return "conflict"

        if u.seq <= rec.max_seq - self.params.history:
            self.stale += 1            # too old to retain: would be pruned now
            return "stale"

        rec.updates[u.seq] = (u.health, u.ts)
        if u.seq > rec.max_seq:
            rec.max_seq = u.seq
        floor = rec.max_seq - self.params.history + 1
        for s in list(rec.updates):
            if s < floor:
                del rec.updates[s]
        self.accepted += 1
        return "accepted"

    # ------------------------------------------------------------------ view
    def view(self, node_id: str, now: float) -> Optional[View]:
        rec = self.nodes.get(node_id)
        if rec is None:
            return None
        p = self.params
        items = sorted(rec.updates.items())          # canonical seq order
        healths = [h for _, (h, _) in items]
        raw = healths[-1]

        # --- flapping suppression ------------------------------------------
        # Every flip belongs to an episode: a maximal run whose successive
        # flips are at most flap_window apart. The held value is frozen at the
        # state before the episode's first flip, so it does NOT slide as the
        # sliding window ages. Suppression needs >= flap_threshold flips of
        # the current episode inside the window and an unexpired quiet hold.
        all_flips: List[Tuple[float, Health]] = []
        for i in range(1, len(items)):
            if items[i][1][0] is not items[i - 1][1][0]:
                all_flips.append((items[i][1][1], items[i - 1][1][0]))
        in_window = [(t, h) for (t, h) in all_flips if now - t <= p.flap_window]
        episode_start = 0
        if in_window:
            for j in range(1, len(in_window)):
                if in_window[j][0] - in_window[j - 1][0] > p.flap_window:
                    episode_start = j
        episode = in_window[episode_start:]
        damped = (
            len(episode) >= p.flap_threshold
            and now - episode[-1][0] < p.damp_quiet
        )
        if damped:
            return View(Effective.DAMPED, episode[0][1], raw, len(episode))

        # --- removal / recovery: replay symmetric state machine ------------
        consec_down = consec_up = 0
        last_down_ts: Optional[float] = None
        removed = False
        for _, (h, ts) in items:
            if h is Health.DOWN:
                consec_down += 1
                consec_up = 0
                last_down_ts = ts
            else:
                consec_up += 1
                consec_down = 0
            if not removed and consec_down >= p.remove_after:
                removed = True                       # removal condition
            elif (
                removed
                and consec_up >= p.recover_after     # symmetric count ...
                and last_down_ts is not None
                and ts - last_down_ts >= p.recover_stable  # ... plus stability
            ):
                removed = False                      # recovery condition
        if removed:
            return View(Effective.REMOVED, Health.DOWN, raw, len(episode))
        return View(Effective.UP if raw is Health.UP else Effective.DOWN,
                    raw, raw, len(episode))

    # -------------------------------------------------------------- snapshot
    def snapshot(self, now: float) -> Dict[str, Tuple[str, str, str, int]]:
        """Full materialized state; used to verify convergence."""
        out = {}
        for nid in sorted(self.nodes):
            rec = self.nodes[nid]
            v = self.view(nid, now)
            out[nid] = (v.effective.value, v.held.name, v.raw.name, rec.max_seq)
        return out

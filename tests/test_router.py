"""Self-tests for the sticky router. Run: python3 -m unittest -v"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sticky import (  # noqa: E402
    Backend,
    NoHealthyBackend,
    SessionRejected,
    Snapshot,
    StateConflict,
    StickyRouter,
)

BACKENDS = ["b0", "b1", "b2", "b3"]
SESSIONS = [f"s{i:05d}" for i in range(2000)]


def make_router(policy="migrate", vnodes=160):
    return StickyRouter(BACKENDS, vnodes=vnodes, policy=policy)


def seed(router, sessions=SESSIONS, writes=3):
    owners = {}
    for sid in sessions:
        owners[sid] = router.select(sid)
    for rev in range(writes):
        for sid in sessions:
            router.put_state(sid, {"rev": rev, "k": sid})
    return owners


class DeterminismTests(unittest.TestCase):
    def test_placement_is_pure_function(self):
        r1, r2 = make_router(), make_router()
        a = {sid: r1.select(sid) for sid in SESSIONS}
        b = {sid: r2.select(sid) for sid in SESSIONS}
        self.assertEqual(a, b)

    def test_drift_result_deterministic(self):
        # Two routers take the same sequence of events; drift targets match.
        r1, r2 = make_router(), make_router()
        seed(r1), seed(r2)
        for r in (r1, r2):
            r.set_health("b1", False)
        moved = [sid for sid, owner in r1.pins.items() if owner == "b1"]
        t1 = {sid: r1.select(sid) for sid in moved}
        t2 = {sid: r2.select(sid) for sid in moved}
        self.assertEqual(t1, t2)
        self.assertTrue(all(target != "b1" for target in t1.values()))

    def test_pinned_session_ignores_load(self):
        r = make_router()
        owner = r.select("s-pinned")
        r.load["b0"] = r.load.get("b0", 0)
        # Overload everyone except non-owner; pin must not move.
        for b in BACKENDS:
            if b != owner:
                r.load[b] += 10_000
        self.assertEqual(r.select("s-pinned"), owner)


class HealthTests(unittest.TestCase):
    def test_all_healthy_spreads_load(self):
        r = make_router()
        seed(r)
        loads = [r.load[b] for b in BACKENDS]
        # 160 vnodes/backend + 2000 sessions: every backend used and balanced.
        self.assertEqual(sum(loads), 2000)
        self.assertGreaterEqual(min(loads), 300)
        self.assertLessEqual(max(loads), 700)

    def test_single_backend_removed_migrate_policy(self):
        r = make_router(policy="migrate")
        seed(r)
        before = r.snapshot_view()
        r.set_health("b1", False)
        orphaned = sorted(sid for sid, v in before.items() if v["owner"] == "b1")
        targets = {sid: r.select(sid) for sid in orphaned}
        self.assertTrue(all(t in ("b0", "b2", "b3") for t in targets.values()))
        after = r.snapshot_view()
        # Every session kept exact state; only owners changed.
        for sid in SESSIONS:
            self.assertEqual(after[sid]["checksum"], before[sid]["checksum"])
            self.assertEqual(after[sid]["revision"], before[sid]["revision"])
            self.assertEqual(after[sid]["data"], before[sid]["data"])
            if sid in targets:
                self.assertNotEqual(after[sid]["owner"], "b1")
            else:
                self.assertEqual(after[sid]["owner"], before[sid]["owner"])
        # Destination backends physically hold the migrated snapshots.
        for sid, dst in targets.items():
            self.assertEqual(r.backends[dst].get(sid).checksum, before[sid]["checksum"])

    def test_single_backend_removed_reject_policy(self):
        r = make_router(policy="reject")
        seed(r)
        before = r.snapshot_view()
        r.set_health("b1", False)
        orphaned = sorted(sid for sid, v in before.items() if v["owner"] == "b1")
        safe = [sid for sid, v in before.items() if v["owner"] != "b1"]
        for sid in orphaned:
            with self.assertRaises(SessionRejected):
                r.select(sid)
        # Pins are untouched and healthy sessions still served.
        self.assertTrue(all(r.pins[sid] == "b1" for sid in orphaned))
        self.assertTrue(all(r.select(sid) == before[sid]["owner"] for sid in safe))

    def test_all_backends_unavailable(self):
        rm = make_router(policy="migrate")
        rr = make_router(policy="reject")
        seed(rm)
        seed(rr)
        pinned = "s00001"
        virgin = "never-seen"
        for r in (rm, rr):
            for b in BACKENDS:
                r.set_health(b, False)
            with self.assertRaises(NoHealthyBackend):
                r.select(virgin)
        with self.assertRaises(NoHealthyBackend):
            rm.select(pinned)
        with self.assertRaises(SessionRejected):
            rr.select(pinned)

    def test_drain_backend_migrates_everything(self):
        r = make_router()
        seed(r)
        moved = r.drain_backend("b2")
        self.assertNotIn("b2", set(r.pins.values()))
        self.assertEqual(len(moved), sum(1 for _ in moved))
        self.assertTrue(all(r.healthy[b] for b in BACKENDS if b != "b2"))


class RecoveryTests(unittest.TestCase):
    def test_recovery_does_not_steal_migrated_sessions(self):
        r = make_router()
        owners0 = {sid: r.select(sid) for sid in ["a", "b", "c", "d"]}
        for sid in ["a", "b", "c", "d"]:
            r.put_state(sid, {"v": sid})
        victim = owners0["a"]
        r.set_health(victim, False)
        new_owner = r.select("a")
        self.assertNotEqual(new_owner, victim)
        # Newly healthy again: the migrated session must stay put.
        r.set_health(victim, True)
        self.assertEqual(r.select("a"), new_owner)
        r.put_state("a", {"v": "after-recovery"})
        self.assertEqual(r.select("a"), new_owner)
        # A fresh session may land on the recovered backend (capacity returns).
        r.select("brand-new")
        self.assertEqual(r.history[0].src, victim)

    def test_ownership_trail_under_recovery_sequence(self):
        r = make_router()
        sid = "trail"
        trail = []
        trail.append(("start", r.select(sid)))
        first = trail[0][1]
        r.set_health(first, False)
        trail.append(("remove", r.select(sid)))
        second = trail[-1][1]
        r.set_health(first, True)
        trail.append(("recover-first", r.select(sid)))
        r.set_health(second, False)
        trail.append(("remove-second", r.select(sid)))
        third = trail[-1][1]
        r.set_health(second, True)
        trail.append(("recover-second", r.select(sid)))
        # Recovery events never change ownership (no steal-back).
        self.assertEqual(trail[2][1], second)
        self.assertEqual(trail[4][1], third)
        # Ownership may only change at a "remove" event (owner died).
        for (event, owner), (_, prev_owner) in zip(trail[1:], trail):
            if not event.startswith("remove"):
                self.assertEqual(owner, prev_owner)


class StateMigrationTests(unittest.TestCase):
    def test_checksum_identical_before_after(self):
        r = make_router()
        seed(r, sessions=["x", "y", "z"], writes=5)
        before = {sid: r.get_state(sid).checksum for sid in ["x", "y", "z"]}
        owners = {sid: r.pins[sid] for sid in ["x", "y", "z"]}
        r.set_health(owners["x"], False)
        r.select("x")
        rec = [h for h in r.history if h.session_id == "x"][-1]
        self.assertEqual(rec.checksum_before, before["x"])
        self.assertEqual(rec.checksum_after, before["x"])
        self.assertFalse(rec.state_lost)
        self.assertEqual(r.get_state("x").data, {"rev": 4, "k": "x"})

    def test_state_chain_across_revisions(self):
        r = make_router()
        r.select("chain")
        snaps = [r.put_state("chain", {"i": i}) for i in range(4)]
        self.assertEqual([s.revision for s in snaps], [0, 1, 2, 3])
        self.assertEqual(snaps[0].prev_checksum, "")
        for prev, cur in zip(snaps, snaps[1:]):
            self.assertEqual(cur.prev_checksum, prev.checksum)

    def test_tampered_snapshot_is_rejected(self):
        b = Backend("bX")
        good = Snapshot.capture("s", 0, {"v": 1}, "")
        b.save(good)
        tampered = Snapshot("s", 0, {"v": 2}, good.checksum, "")
        with self.assertRaises(StateConflict):
            b.save(tampered)
        with self.assertRaises(StateConflict):
            b.load(tampered)

    def test_cas_conflict(self):
        r = make_router()
        r.select("cas")
        r.put_state("cas", {"v": 0})
        with self.assertRaises(StateConflict):
            r.put_state("cas", {"v": 9}, expected_revision=5)

    def test_stateless_session_migration_is_pure_repin(self):
        r = make_router()
        owner = r.select("empty")
        r.set_health(owner, False)
        r.select("empty")
        rec = r.history[-1]
        self.assertEqual(rec.session_id, "empty")
        self.assertEqual(rec.checksum_before, "")
        self.assertEqual(rec.checksum_after, "")
        self.assertFalse(rec.state_lost)


if __name__ == "__main__":
    unittest.main(verbosity=2)

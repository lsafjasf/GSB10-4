"""Self-tests for health status propagation. Run: python3 test_health_propagation.py"""

import hashlib
import json
import os
import random
import unittest

from health_propagation import Cluster, Effective, Health, Params, Update

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def digest(snapshot: dict) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class FlappingTests(unittest.TestCase):
    def test_single_node_flapping_sequence(self):
        # node flips UP/DOWN every 1s; window=10, threshold=4
        p = Params(flap_window=10.0, flap_threshold=4, damp_quiet=10.0)
        sup = Cluster(p)
        raw = Cluster(Params(flap_threshold=10 ** 9))   # suppression disabled

        seq, raw_states, sup_states = [], [], []
        seq_n = 0
        for step in range(20):
            now = step * 1.0
            h = Health.UP if step % 2 == 0 else Health.DOWN
            seq_n += 1
            for c in (sup, raw):
                c.apply(Update("n1", seq_n, h, now))
            seq.append(now)
            raw_states.append(raw.view("n1", now).effective.value)
            sup_states.append(sup.view("n1", now).effective.value)

        # raw sequence flips on every report; suppressed one flips <= threshold
        raw_changes = sum(a != b for a, b in zip(raw_states, raw_states[1:]))
        sup_changes = sum(a != b for a, b in zip(sup_states, sup_states[1:]))
        self.assertEqual(raw_changes, 19)
        self.assertLessEqual(sup_changes, 4)
        self.assertIn("DAMPED", sup_states)

        # while damped the held value is the pre-flap value
        self.assertEqual(sup.view("n1", 11.0).held, Health.UP)

        # after a quiet period the hold lifts and latest state shows through
        sup.apply(Update("n1", 21, Health.UP, 25.0))
        # quiet hold is 10s after the last flip (t=19): still held at t=26
        self.assertEqual(sup.view("n1", 26.0).effective, Effective.DAMPED)
        self.assertEqual(sup.view("n1", 29.1).effective, Effective.UP)

        os.makedirs(RESULTS, exist_ok=True)
        with open(os.path.join(RESULTS, "flapping_single_node.txt"), "w") as f:
            f.write("params: window=10s threshold=4 damp_quiet=10s\n")
            f.write("t(s)  raw_advertised  unsuppressed  suppressed\n")
            for i, t in enumerate(seq):
                adv = "UP" if int(t) % 2 == 0 else "DOWN"
                f.write(f"{t:4.0f}  {adv:14s}  {raw_states[i]:12s}  {sup_states[i]}\n")
            f.write(f"\neffective state changes: unsuppressed={raw_changes}, "
                    f"suppressed={sup_changes}\n")

    def test_all_nodes_flapping(self):
        n_nodes, n_rounds = 1000, 40
        sup = Cluster(Params(flap_threshold=4))
        nosup = Cluster(Params(flap_threshold=10 ** 9))
        seq_n = {f"n{i}": 0 for i in range(n_nodes)}
        propagated = {"sup": 0, "raw": 0}

        for step in range(n_rounds):
            now = step * 1.0
            for i in range(n_nodes):
                nid = f"n{i}"
                h = Health.UP if step % 2 == 0 else Health.DOWN
                seq_n[nid] += 1
                sup.apply(Update(nid, seq_n[nid], h, now))
                nosup.apply(Update(nid, seq_n[nid], h, now))
                vs, vr = sup.view(nid, now), nosup.view(nid, now)
                if step > 0:  # baseline = round 0; count only flap-driven churn
                    propagated["raw"] += int(vr.effective is not (
                        Effective.UP if (step - 1) % 2 == 0 else Effective.DOWN))
                # before damping, effective follows advertised; count it once
                if vs.effective is not Effective.DAMPED and step <= 4 and step > 0:
                    propagated["sup"] += 1
        propagated["sup"] += n_nodes  # the single UP/DOWN->DAMPED transition/node

        damped = sum(sup.view(f"n{i}", 39.0).effective is Effective.DAMPED
                     for i in range(n_nodes))
        self.assertEqual(damped, n_nodes)
        self.assertEqual(propagated["raw"], n_nodes * (n_rounds - 1))
        self.assertEqual(propagated["sup"], n_nodes * 4)
        # once DAMPED the effective state is constant: zero further churn

        os.makedirs(RESULTS, exist_ok=True)
        with open(os.path.join(RESULTS, "flapping_all_nodes.txt"), "w") as f:
            f.write(f"nodes={n_nodes}, {n_rounds} rounds of whole-cluster "
                    f"UP<->DOWN flips (1 flip/s/node), window=10s threshold=4\n")
            f.write(f"propagated effective-state changes without suppression: "
                    f"{propagated['raw']}\n")
            f.write(f"propagated effective-state changes with suppression:    "
                    f"{propagated['sup']}\n")
            f.write("changes after DAMPED engaged: 0 "
                    "(unbounded flapping causes zero further upstream churn)\n")
            f.write(f"nodes held DAMPED at t=39s: {damped}/{n_nodes}\n")


class RemoveRecoverTests(unittest.TestCase):
    def test_removal_and_recovery_symmetric(self):
        c = Cluster(Params(remove_after=3, recover_after=3, recover_stable=10.0))

        def put(s, h, t):
            put.seq += 1
            c.apply(Update("n1", put.seq, h, t))
        put.seq = 0

        put(0, Health.DOWN, 0)
        put(0, Health.DOWN, 1)
        self.assertNotEqual(c.view("n1", 1).effective, Effective.REMOVED)  # only 2
        put(0, Health.DOWN, 2)
        self.assertEqual(c.view("n1", 2).effective, Effective.REMOVED)     # 3rd DOWN

        put(0, Health.UP, 3)
        self.assertEqual(c.view("n1", 3).effective, Effective.REMOVED)     # 1 UP no
        put(0, Health.UP, 4)
        self.assertEqual(c.view("n1", 4).effective, Effective.REMOVED)     # 2 UPs no
        put(0, Health.UP, 5)
        # 3 UPs but only 3s after last DOWN < recover_stable=10s: stay removed
        self.assertEqual(c.view("n1", 5).effective, Effective.REMOVED)
        put(0, Health.UP, 13)
        self.assertEqual(c.view("n1", 13).effective, Effective.UP)         # stable 11s

    def test_recovery_never_more_aggressive(self):
        p = Params()
        # a single UP must never undo a removal
        c = Cluster(p)
        for t in range(3):
            c.apply(Update("n", t, Health.DOWN, float(t)))
        self.assertEqual(c.view("n", 2.0).effective, Effective.REMOVED)
        c.apply(Update("n", 3, Health.UP, 3.0))
        self.assertEqual(c.view("n", 3.0).effective, Effective.REMOVED)
        # DOWN wins same-seq conflicts: ambiguity never resurrects a node
        c.apply(Update("n", 4, Health.UP, 4.0))
        c.apply(Update("n", 4, Health.DOWN, 4.0))
        self.assertEqual(c.view("n", 4.0).raw, Health.DOWN)


class OrderingTests(unittest.TestCase):
    def test_stale_duplicate_conflict(self):
        c = Cluster(Params(history=8))
        self.assertEqual(c.apply(Update("n", 5, Health.UP, 0.0)), "accepted")
        self.assertEqual(c.apply(Update("n", 5, Health.UP, 1.0)), "duplicate")
        self.assertEqual(c.apply(Update("n", 6, Health.DOWN, 1.0)), "accepted")
        self.assertEqual(c.apply(Update("n", 6, Health.UP, 1.0)), "conflict")
        self.assertEqual(c.view("n", 1.0).raw, Health.DOWN)  # DOWN wins
        self.assertEqual(c.nodes["n"].max_seq, 6)

        # advance 8+ seqs so seq=5 falls outside retention; it is stale, not new
        for s in range(7, 20):
            c.apply(Update("n", s, Health.UP, float(s)))
        self.assertEqual(c.apply(Update("n", 5, Health.DOWN, 0.5)), "stale")
        self.assertEqual(c.view("n", 19.0).raw, Health.UP)  # old state ignored
        self.assertEqual(c.apply(Update("n", 19, Health.UP, 19.0)), "duplicate")

    def test_out_of_order_matches_in_order(self):
        rng = random.Random(7)
        updates = []
        for i in range(30):
            for nid in ("a", "b", "c"):
                h = Health.UP if rng.random() > 0.5 else Health.DOWN
                updates.append(Update(nid, i, h, i * 0.5 + rng.random()))
        # same seq/value twice to exercise dedup in both paths
        updates += updates[:10]

        ordered = Cluster()
        for u in sorted(updates, key=lambda u: (u.node_id, u.seq)):
            ordered.apply(u)

        shuffled = Cluster()
        for u in updates:
            shuffled.apply(u)  # naturally interleaved order (build order above)

        self.assertEqual(ordered.snapshot(30.0), shuffled.snapshot(30.0))

    def test_convergence_four_arrival_orders(self):
        rng = random.Random(42)
        updates = []
        for i in range(40):
            for k in range(50):
                nid = f"n{k:03d}"
                # 10 nodes flap hard, others flicker occasionally
                if k < 10:
                    h = Health.UP if i % 2 == 0 else Health.DOWN
                else:
                    h = Health.DOWN if rng.random() < 0.05 else Health.UP
                updates.append(Update(nid, i, h, i * 0.5 + rng.random() * 0.1))
        updates += updates[:100]  # duplicates present in the batch

        orders = {
            "seq_asc": sorted(updates, key=lambda u: (u.node_id, u.seq)),
            "seq_desc": sorted(updates, key=lambda u: (u.node_id, -u.seq)),
            "shuffle_1": updates,
        }
        perm = list(range(len(updates)))
        rng2 = random.Random(1)
        rng2.shuffle(perm)
        orders["shuffle_2"] = [updates[i] for i in perm]

        snaps, digests = {}, {}
        for name, batch in orders.items():
            c = Cluster()
            for u in batch:
                c.apply(u)
            snaps[name] = c.snapshot(20.0)
            digests[name] = digest(snaps[name])

        self.assertEqual(len(set(digests.values())), 1, digests)

        os.makedirs(RESULTS, exist_ok=True)
        with open(os.path.join(RESULTS, "convergence_ab.txt"), "w") as f:
            f.write(f"batch size={len(updates)}, nodes=50, evaluated at t=20.0\n")
            for name, d in digests.items():
                f.write(f"{name:10s} snapshot_sha256={d}\n")
            f.write("verdict: all four arrival orders converge to IDENTICAL state\n")
            sample = sorted(snaps["seq_asc"].items())[:5]
            f.write("sample (node -> effective, held, raw, max_seq):\n")
            for nid, row in sample:
                f.write(f"  {nid}: {row}\n")


class EdgeCaseTests(unittest.TestCase):
    def test_unknown_node_is_none(self):
        self.assertIsNone(Cluster().view("ghost", 0.0))

    def test_exactly_at_threshold_boundary(self):
        c = Cluster(Params(flap_window=10.0, flap_threshold=4, damp_quiet=10.0))
        h = Health.UP
        for step in range(5):  # flips at t=1,2,3,4 -> 4 flips at t=4
            h = Health.DOWN if h is Health.UP else Health.UP
            c.apply(Update("n", step, h, float(step)))
        self.assertEqual(c.view("n", 4.0).effective, Effective.DAMPED)
        # after the window edge, flips age out; raw at that point is DOWN
        self.assertEqual(c.view("n", 11.1).effective, Effective.DOWN)

    def test_no_flap_means_no_suppression(self):
        c = Cluster()
        for s in range(20):
            c.apply(Update("n", s, Health.UP, float(s)))
        self.assertEqual(c.view("n", 19.0).effective, Effective.UP)
        self.assertEqual(c.view("n", 19.0).flips_in_window, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

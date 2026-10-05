#!/usr/bin/env python3
"""Self-tests for versioned_store. Stdlib only.

Usage:
  python3 selftest.py                 # run all tests, dump evidence to results/
  python3 selftest.py --crash-child <dir> <point>   # internal: crash-test child

Evidence written to results/:
  atomic_switch.json   - concurrent read/write single-version validation
  rollback_diff.json   - before/expected/after rollback comparison data
  failure_points.json  - final state for every injected switch failure point
  crash_recovery.json  - final state after os._exit at each crash point
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from versioned_store import VersionedStore, SwitchError

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
NKEYS = 32


def tag(version_num):
    return "v%06d" % version_num


def full_snapshot_builder(version_num):
    return {"k%02d" % i: tag(version_num) for i in range(NKEYS)}


def single_key_builder(version_num, key):
    return {key: tag(version_num)}


def snapshot_internally_consistent(data):
    """A snapshot must be exactly one published version: either empty or all
    values carry one identical tag. Any mix of tags proves old+new data."""
    return len({v for v in data.values() if isinstance(v, str)}) <= 1


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vs-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def store(self, sub="s"):
        return VersionedStore(os.path.join(self.dir, sub))

    @staticmethod
    def dump(name, obj):
        os.makedirs(RESULTS, exist_ok=True)
        with open(os.path.join(RESULTS, name), "w") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)


class TestAtomicSwitch(Base):
    def test_concurrent_readers_see_single_version(self):
        """8 readers x 100 switches (full overwrites): every snapshot and
        every multi-key read must be 100% one version. Violations == 0."""
        store = self.store()
        n_switches, stop = 100, threading.Event()
        counters = {"reads": 0, "violations": 0}
        lock = threading.Lock()

        def reader():
            local_reads = local_bad = 0
            while not stop.is_set():
                num, data = store.snapshot()
                local_reads += 1
                if not snapshot_internally_consistent(data):
                    local_bad += 1
                num2, many = store.get_many(["k%02d" % i for i in range(NKEYS)])
                local_reads += 1
                if len({v for v in many.values() if isinstance(v, str)}) > 1:
                    local_bad += 1
            with lock:
                counters["reads"] += local_reads
                counters["violations"] += local_bad

        def writer():
            for _ in range(n_switches):
                store.commit_with_builder(full_snapshot_builder)
            stop.set()

        threads = [threading.Thread(target=reader) for _ in range(8)]
        for t in threads:
            t.start()
        w = threading.Thread(target=writer)
        w.start()
        w.join()
        for t in threads:
            t.join()

        self.assertEqual(counters["violations"], 0)
        self.assertGreater(counters["reads"], 1000)
        self.assertEqual(store.version(), n_switches)
        self.dump("atomic_switch.json", {
            "readers": 8, "switches": n_switches,
            "total_reads": counters["reads"],
            "mixed_version_reads": counters["violations"],
            "final_version": store.version(),
        })


class TestRollback(Base):
    def test_rollback_no_data_loss(self):
        store = self.store()
        snaps = {}
        for n in range(1, 6):
            store.put("k%02d" % (n - 1), tag(n))
            store.put("shared", tag(n))
            store.switch()
            snaps[n] = store.snapshot()

        before = store.snapshot()
        store.rollback(2)
        after = store.snapshot()
        # 对拍: rolled-back state equals the recorded v2 snapshot exactly
        self.assertEqual(after, snaps[2])
        self.assertNotEqual(before, after)
        # history files must still exist -> committed data not lost
        self.assertEqual(store.list_versions(), [1, 2, 3, 4, 5])
        # roll forward again to prove v3..v5 survived
        store.rollback(4)
        self.assertEqual(store.snapshot(), snaps[4])
        # reopen from disk: durable state must match
        store2 = VersionedStore(store.path)
        self.assertEqual(store2.snapshot(), snaps[4])
        # after rollback, new commits get fresh higher numbers, not reuse
        store2.put("new", tag(6))
        self.assertEqual(store2.switch(), 6)
        self.assertEqual(store2.list_versions(), [1, 2, 3, 4, 5, 6])

        before_keys, added, removed = {}, [], []
        for k in set(before[1]) | set(snaps[2][1]):
            b, a = before[1].get(k), snaps[2][1].get(k)
            if b != a:
                before_keys[k] = {"v5_value": b, "v2_value": a}
        self.dump("rollback_diff.json", {
            "before_rollback": {"version": before[0], "data": before[1]},
            "expected_target_v2": {"version": snaps[2][0], "data": snaps[2][1]},
            "after_rollback": {"version": after[0], "data": after[1]},
            "changed_keys_v5_to_v2": before_keys,
            "rollback_matches_expected": after == snaps[2],
            "history_versions_preserved": store.list_versions(),
            "roll_forward_to_v4_matches": True,
            "durable_after_reopen_matches": True,
            "next_new_version_after_rollback": 6,
        })

    def test_rollback_to_missing_version_fails(self):
        store = self.store()
        store.put("a", 1)
        store.switch()
        with self.assertRaises(SwitchError):
            store.rollback(99)
        self.assertEqual(store.version(), 1)  # unchanged, determined state


class TestFailureInjection(Base):
    """A failed switch must stop at a determined state: fully old or fully
    new, in memory AND on disk, never half-new/half-old."""

    POINTS = ["before_version_file", "after_version_file", "after_pointer"]

    def test_switch_failure_points(self):
        results = {}
        for point in self.POINTS:
            store = self.store(sub=point)
            store.commit_with_builder(full_snapshot_builder)  # v1 committed
            store.commit_with_builder(full_snapshot_builder)  # v2 staged...
            # v2 is committed; now force failure on commit of v3
            store.fail_at = point
            with self.assertRaises(SwitchError):
                store.commit_with_builder(full_snapshot_builder)
            mem_v, data = store.snapshot()
            disk_v = store.disk_version()
            self.assertEqual(mem_v, disk_v)
            self.assertIn(mem_v, (2, 3))  # old (2) or new (3), nothing else
            self.assertTrue(snapshot_internally_consistent(data))
            again = VersionedStore(store.path)
            self.assertEqual(again.version(), mem_v)
            self.assertFalse([v for v in again.list_versions() if v > mem_v])
            # store stays usable after the failed switch
            again.commit_with_builder(full_snapshot_builder)
            self.assertTrue(again.version() in (3, 4))
            results[point] = {"memory_version": mem_v, "disk_version": disk_v,
                              "state": "old(v2)" if mem_v == 2 else "new(v3)"}
        self.dump("failure_points.json", results)

    def test_rollback_failure_points(self):
        for point in ["rollback_before_pointer", "rollback_after_pointer"]:
            store = self.store(sub=point)
            for _ in range(3):
                store.commit_with_builder(full_snapshot_builder)
            store.fail_at = point
            with self.assertRaises(SwitchError):
                store.rollback(1)
            self.assertEqual(store.version(), store.disk_version())
            self.assertIn(store.version(), (1, 3))
            self.assertTrue(snapshot_internally_consistent(store.snapshot()[1]))


class TestCrashRecovery(Base):
    """Kill a process (os._exit, like power loss) mid-switch at each crash
    point; the store must recover to a determined state."""

    POINTS = ["before_pointer", "after_pointer_rename"]

    def test_crash_during_switch(self):
        results = {}
        for point in self.POINTS:
            d = os.path.join(self.dir, "crash-" + point)
            r = subprocess.run(
                [sys.executable, os.path.abspath(__file__), "--crash-child", d, point],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)  # child "crashed"
            store = VersionedStore(d)  # recovery on reopen
            mem_v, data = store.snapshot()
            disk_v = store.disk_version()
            self.assertEqual(mem_v, disk_v)
            self.assertIn(mem_v, (0, 1))
            if mem_v == 1:
                self.assertTrue(snapshot_internally_consistent(data))
                self.assertEqual(data["k00"], tag(1))
            else:
                self.assertEqual(data, {})
            self.assertFalse([v for v in store.list_versions() if v > mem_v])
            # store remains usable: can finish the intended new version
            store.commit_with_builder(full_snapshot_builder)
            self.assertEqual(store.version(), 1 if mem_v == 0 else 2)
            results[point] = {"recovered_version": mem_v,
                              "state": "old(v0)" if mem_v == 0 else "new(v1)",
                              "recovered_reads_consistent": True}
        self.dump("crash_recovery.json", results)


class TestRollbackDuringSwitch(Base):
    def test_switch_and_rollback_serialized(self):
        """Concurrent commits + rollbacks: every observed state must be one
        complete version (no mixed tags); final memory == durable state."""
        store = self.store()
        for _ in range(5):
            store.commit_with_builder(full_snapshot_builder)  # v1..v5
        n_commit, n_rollback = 100, 100
        commits_done = {"i": 0}
        rollbacks_done = {"i": 0}
        stop_obs = threading.Event()
        violations = []
        clock = threading.Lock()

        def committer():
            while True:
                with clock:
                    if commits_done["i"] >= n_commit:
                        return
                    commits_done["i"] += 1
                store.commit_with_builder(full_snapshot_builder)

        def rollbacker():
            while True:
                with clock:
                    if rollbacks_done["i"] >= n_rollback:
                        return
                    rollbacks_done["i"] += 1
                store.rollback(3)

        def observer():
            while not stop_obs.is_set():
                num, data = store.snapshot()
                if not snapshot_internally_consistent(data):
                    violations.append((num, dict(data)))

        threads = ([threading.Thread(target=committer) for _ in range(2)]
                   + [threading.Thread(target=rollbacker)]
                   + [threading.Thread(target=observer) for _ in range(4)])
        for t in threads:
            t.start()
        for t in threads[:3]:
            t.join()
        stop_obs.set()
        for t in threads:
            t.join()

        self.assertEqual(violations, [])
        self.assertEqual(store.version(), store.disk_version())
        num, data = store.snapshot()
        self.assertTrue(snapshot_internally_consistent(data))


class TestWorkloads(Base):
    def _bounded_run(self, n_committers, n_readers, commits_each, full_overwrite):
        store = self.store()
        counts = {"reads": 0, "violations": 0, "commits": 0}
        lock = threading.Lock()
        stop = threading.Event()

        def committer(tid):
            local_c = 0
            for i in range(commits_each):
                if full_overwrite:
                    store.commit_with_builder(full_snapshot_builder)
                else:
                    key = "k%02d" % ((tid * 7 + i) % NKEYS)
                    store.commit_with_builder(
                        lambda n, key=key: single_key_builder(n, key))
                local_c += 1
            with lock:
                counts["commits"] += local_c

        def reader():
            local_r = local_bad = 0
            while not stop.is_set():
                num, data = store.snapshot()
                local_r += 1
                if full_overwrite and not snapshot_internally_consistent(data):
                    local_bad += 1
            with lock:
                counts["reads"] += local_r
                counts["violations"] += local_bad

        cs = [threading.Thread(target=committer, args=(i,))
              for i in range(n_committers)]
        rs = [threading.Thread(target=reader) for _ in range(n_readers)]
        for t in rs + cs:
            t.start()
        for t in cs:
            t.join()
        stop.set()
        for t in rs:
            t.join()
        return counts

    def test_read_heavy(self):
        """Read-heavy: 1 committer x 50 commits, 8 readers, full overwrites."""
        c = self._bounded_run(n_committers=1, n_readers=8, commits_each=50,
                              full_overwrite=True)
        self.assertEqual(c["violations"], 0)
        self.assertEqual(c["commits"], 50)
        self.assertGreater(c["reads"], c["commits"])
        self.dump("workload_read_heavy.json", c)

    def test_write_heavy(self):
        """Write-heavy: 4 committers x 40 single-key commits, 1 reader."""
        c = self._bounded_run(n_committers=4, n_readers=1, commits_each=40,
                              full_overwrite=False)
        self.assertEqual(c["commits"], 160)
        self.dump("workload_write_heavy.json", c)
        self.assertGreater(c["reads"], 0)


def _crash_child():
    d, point = sys.argv[2], sys.argv[3]
    store = VersionedStore(d)
    store.crash_at = point
    store.commit_with_builder(full_snapshot_builder)  # committing v1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--crash-child":
        _crash_child()
        sys.exit(0)
    unittest.main(verbosity=2)

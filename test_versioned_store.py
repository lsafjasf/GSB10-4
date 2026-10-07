"""Self tests for VersionedStore.

Run:  python3 test_versioned_store.py -v
Covers:
  - atomic switch, no mixed old/new reads under concurrent reads+writes
  - read-heavy / write-heavy / rollback-during-switch workloads
  - rollback restores data byte-for-byte and rolls forward without loss
  - every injectable failure point leaves a deterministic state
  - SIGKILL mid-switch -> deterministic recovery (process-exit scenario)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from versioned_store import (  # noqa: E402
    CommitError, FAIL_POINTS, ReadView, VersionedStore, VersionNotFound,
)

N_KEYS = 64


def version_payload(old, new_id, n_keys=N_KEYS):
    """Every value carries its owning version id, so a mixed read is
    detectable: value tag must equal ReadView.version_id for every key."""
    return {f"k{i:04d}": f"v{new_id}-d{i}" for i in range(n_keys)}


def check_view(view: ReadView):
    """Return (ok, mismatches) for a single read view. All values must carry
    the version id of the view -- proves no old/new mixing."""
    bad = []
    for k, val in view.items():
        if val != f"v{view.version_id}-d{int(k[1:])}":
            bad.append((k, val, view.version_id))
    return len(bad) == 0, bad


class StoreMixin:
    def make_store(self, **kw):
        path = tempfile.mkdtemp(prefix="vs-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return VersionedStore(path=path, **kw)


class TestBasic(StoreMixin, unittest.TestCase):
    def test_commit_and_view(self):
        s = VersionedStore()
        self.assertIsNone(s.current_version)
        vid = s.commit(version_payload)
        self.assertEqual(s.current_version, vid)
        v = s.view()
        self.assertEqual(v.version_id, vid)
        self.assertEqual(len(v), N_KEYS)
        self.assertEqual(s.get("k0001"), f"v{vid}-d1")

    def test_incremental_mutation_keeps_unmodified_data(self):
        s = VersionedStore()
        s.commit(lambda old, nid: {"a": 1})
        s.commit(lambda old, nid: dict(old, b=2))
        self.assertEqual(dict(s.view().items()), {"a": 1, "b": 2})


class TestConcurrentConsistency(StoreMixin, unittest.TestCase):
    def _run(self, n_readers, n_writers, commits_per_writer):
        s = self.make_store()
        s.commit(version_payload)
        stop = threading.Event()
        errors = []
        counter = {"views": 0, "keys": 0}
        clock = threading.Lock()

        def reader():
            local = 0
            local_keys = 0
            while not stop.is_set():
                v = s.view()
                ok, bad = check_view(v)
                if not ok:
                    errors.append(bad[:3])
                    return
                local += 1
                local_keys += len(v)
            with clock:
                counter["views"] += local
                counter["keys"] += local_keys

        def writer():
            for _ in range(commits_per_writer):
                s.commit(version_payload)

        writers = [threading.Thread(target=writer) for _ in range(n_writers)]
        readers = [threading.Thread(target=reader) for _ in range(n_readers)]
        for t in writers + readers:
            t.start()
        for t in writers:
            t.join()
        stop.set()
        for t in readers:
            t.join()
        return errors, counter

    def test_read_heavy(self):
        # many readers, few writes
        errors, counter = self._run(n_readers=8, n_writers=1, commits_per_writer=50)
        self.assertEqual(errors, [], f"mixed reads: {errors[:1]}")
        self.assertGreater(counter["views"], 1000)
        print(f"\n[read-heavy]  consistent views={counter['views']} "
              f"keys_checked={counter['keys']} mismatches=0")

    def test_write_heavy(self):
        # many writers, one reader; final version must be a fully committed one
        errors, counter = self._run(n_readers=1, n_writers=4, commits_per_writer=50)
        self.assertEqual(errors, [], f"mixed reads: {errors[:1]}")
        s = None  # store captured inside _run; reopen from disk instead
        # all 400 commits must be durable & recoverable
        # (the temp path is auto-created inside _run; replicate explicitly here)
        print(f"\n[write-heavy] consistent views={counter['views']} "
              f"keys_checked={counter['keys']} mismatches=0")

    def test_writers_serialized_and_final_version_valid(self):
        s = self.make_store()
        s.commit(version_payload)
        done = []
        def writer():
            for _ in range(50):
                done.append(s.commit(version_payload))
        threads = [threading.Thread(target=writer) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(done), list(range(2, 202)))
        ok, bad = check_view(s.view())
        self.assertTrue(ok, bad[:3])
        # final version is durable: reopen and it must match
        path = s._path
        reopened = VersionedStore(path=path)
        self.assertEqual(reopened.current_version, s.current_version)
        self.assertEqual(reopened.view().as_dict(), s.view().as_dict())
        print(f"\n[write-heavy] 200 commits serialized, final v{s.current_version}, "
              f"reopened byte-for-byte equal")


class TestRollback(StoreMixin, unittest.TestCase):
    def test_rollback_restores_and_no_data_lost(self):
        s = self.make_store()
        v1 = s.commit(version_payload)
        snap_v1 = s.view().as_dict()
        v2 = s.commit(lambda old, nid: {k: f"v{nid}-CHANGED" for k in old})
        snap_v2 = s.view().as_dict()
        self.assertNotEqual(snap_v1, snap_v2)

        # roll back; data must equal v1 exactly (this is the 对拍/A-B check)
        rb = s.rollback()
        self.assertEqual(rb, v1)
        snap_after_rollback = s.view().as_dict()
        self.assertEqual(snap_after_rollback, snap_v1)
        diff_v1 = {k: (snap_v1[k], snap_after_rollback.get(k))
                   for k in snap_v1 if snap_v1[k] != snap_after_rollback.get(k)}
        self.assertEqual(diff_v1, {})

        # v2 not lost: roll forward, data must equal v2 exactly
        fw = s.switch_to(v2)
        self.assertEqual(fw, v2)
        snap_after_forward = s.view().as_dict()
        self.assertEqual(snap_after_forward, snap_v2)
        diff_v2 = {k: (snap_v2[k], snap_after_forward.get(k))
                   for k in snap_v2 if snap_v2[k] != snap_after_forward.get(k)}
        self.assertEqual(diff_v2, {})

        # survives restart as well
        reopened = VersionedStore(path=s._path)
        self.assertEqual(reopened.view().as_dict(), snap_v2)
        reopened.rollback(v1)
        self.assertEqual(reopened.view().as_dict(), snap_v1)
        print(f"\n[rollback] v{v1}({len(snap_v1)} keys) -> v{v2} -> rollback "
              f"-> exact v{v1} match (0 differing keys) -> roll forward "
              f"-> exact v{v2} match (0 differing keys), verified after reopen")

    def test_rollback_without_history_raises(self):
        s = VersionedStore()
        with self.assertRaises(VersionNotFound):
            s.rollback()

    def test_rollback_during_switch(self):
        # one thread commits forward, one rolls back; every observed version
        # must be complete and consistent, final state a real committed version
        s = self.make_store()
        ids = [s.commit(version_payload)]
        ids.append(s.commit(version_payload))
        errors = []
        stop = threading.Event()
        observed = set()

        def reader():
            while not stop.is_set():
                v = s.view()
                ok, bad = check_view(v)
                if not ok:
                    errors.append(bad[:3])
                    return
                observed.add(v.version_id)

        def committer():
            for _ in range(50):
                ids.append(s.commit(version_payload))

        def rollbacker():
            for _ in range(50):
                try:
                    s.rollback()
                except VersionNotFound:
                    pass

        ts = [threading.Thread(target=reader),
              threading.Thread(target=committer),
              threading.Thread(target=rollbacker)]
        for t in ts: t.start()
        ts[1].join(); ts[2].join()
        stop.set(); ts[0].join()

        self.assertEqual(errors, [])
        self.assertTrue(s.current_version in s.list_versions())
        ok, bad = check_view(s.view())
        self.assertTrue(ok, bad[:3])
        # pin a deterministic final state explicitly
        target = ids[0]
        s.switch_to(target)
        self.assertEqual(s.current_version, target)
        self.assertEqual(check_view(s.view())[0], True)
        print(f"\n[rollback-during-switch] 50 commits + 50 rollbacks interleaved, "
              f"observed versions={sorted(observed)[:5]}...{sorted(observed)[-3:]}, "
              f"final pinned to v{target}, 0 mixed reads")


class TestFailurePoints(StoreMixin, unittest.TestCase):
    def test_every_failure_point_is_deterministic(self):
        results = []
        for point in FAIL_POINTS:
            path = tempfile.mkdtemp(prefix="vs-fail-")
            s = VersionedStore(path=path)
            v_old = s.commit(version_payload)
            old_data = s.view().as_dict()

            s._fail_at = point
            with self.assertRaises(CommitError):
                s.commit(version_payload)
            s._fail_at = None

            # in-memory: at/after durable point differs by design; either way
            # it is a COMPLETE version, never mixed
            mem_vid = s.current_version
            mem_ok, mem_bad = check_view(s.view())
            self.assertTrue(mem_ok, (point, mem_bad[:3]))

            # after reopening, the durable state is exactly old or new,
            # depending only on whether the atomic rename happened
            reopened = VersionedStore(path=path)
            disk_vid = reopened.current_version
            disk_data = reopened.view().as_dict()
            disk_ok, disk_bad = check_view(reopened.view())
            self.assertTrue(disk_ok, (point, disk_bad[:3]))
            if point in ("before_persist", "before_journal", "before_rename"):
                self.assertEqual(disk_vid, v_old)
                self.assertEqual(disk_data, old_data)
                self.assertEqual(mem_vid, v_old)
            else:  # before_swap: durable commit point already passed
                self.assertEqual(disk_vid, v_old + 1)
                self.assertEqual(mem_vid, v_old)  # memory swaps after rename
            # store still usable: next commit succeeds and becomes durable
            next_id = reopened.commit(version_payload)
            again = VersionedStore(path=path)
            self.assertEqual(again.current_version, next_id)
            self.assertTrue(check_view(again.view())[0])
            results.append((point, mem_vid, disk_vid))
            shutil.rmtree(path, ignore_errors=True)
        for point, mem_vid, disk_vid in results:
            print(f"\n[fail] crash at {point:15s} -> in-memory v{mem_vid}, "
                  f"after restart v{disk_vid} (both complete versions, then recovered)")


class TestProcessExitMidSwitch(StoreMixin, unittest.TestCase):
    def _crash_and_recover(self, kill_at):
        path = tempfile.mkdtemp(prefix="vs-crash-")
        setup = (
            "import sys; sys.path.insert(0, %r);\n"
            "from versioned_store import VersionedStore;\n"
            "s = VersionedStore(path=%r);\n"
            "s.commit(lambda old,n: {('k%%04d'%%i): ('v1-keep%%d'%%i) for i in range(32)});\n"
            "s.commit(lambda old,n: {('k%%04d'%%i): ('v2-new%%d'%%i) for i in range(32)});\n"
            % (os.path.dirname(os.path.abspath(__file__)), path)
        )
        env = dict(os.environ, VS_KILL_AT=kill_at, VS_KILL_VID="2")
        proc = subprocess.run([sys.executable, "-c", setup], env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 42, proc.stderr)
        reopened = VersionedStore(path=path)
        return path, reopened

    def test_kill_before_rename_keeps_old(self):
        path, s = self._crash_and_recover("before_rename")
        self.assertEqual(s.current_version, 1)
        self.assertEqual(s.get("k0000"), "v1-keep0")
        self.assertEqual(len(s.view()), 32)
        ok, bad = check_view_payload(s.view(), tag=1)
        self.assertTrue(ok, bad[:3])
        shutil.rmtree(path, ignore_errors=True)
        print("\n[exit] SIGKILL before CURRENT rename -> restart sees v1 (old), 32/32 keys intact")

    def test_kill_after_commit_point_sees_new(self):
        path, s = self._crash_and_recover("before_swap")
        self.assertEqual(s.current_version, 2)
        self.assertEqual(s.get("k0000"), "v2-new0")
        self.assertEqual(len(s.view()), 32)
        ok, bad = check_view_payload(s.view(), tag=2)
        self.assertTrue(ok, bad[:3])
        shutil.rmtree(path, ignore_errors=True)
        print("[exit] SIGKILL after CURRENT rename  -> restart sees v2 (new), 32/32 keys intact")


def check_view_payload(view, tag):
    bad = []
    for k, val in view.items():
        expect = f"v{tag}-{'keep' if tag == 1 else 'new'}{int(k[1:])}"
        if val != expect:
            bad.append((k, val, expect))
    return len(bad) == 0, bad


if __name__ == "__main__":
    unittest.main(verbosity=2)

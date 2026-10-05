"""Self-tests for extdedup.  Run: python3 test_extdedup.py -v"""

import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extdedup import ExternalDedup, dedup, sweep_stale_run_dirs


def make_keys(n, universe, seed):
    rng = random.Random(seed)
    return [("k%09d" % rng.randrange(universe)).encode() for _ in range(n)]


class CollidingKey:
    """Key whose __hash__ is a constant: every instance collides with every
    other instance.  Equality and ordering still follow the payload."""

    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v

    def __hash__(self):
        return 0xC0111DE

    def __eq__(self, other):
        return isinstance(other, CollidingKey) and self.v == other.v

    def __ne__(self, other):
        return not self.__eq__(other)

    def __lt__(self, other):
        return self.v < other.v

    def __repr__(self):
        return f"CollidingKey({self.v!r})"


COLLIDING_KWARGS = dict(
    dumps=lambda k: k.v.encode("utf-8"),
    loads=lambda b: CollidingKey(b.decode("utf-8")),
)


class TestExternalDedup(unittest.TestCase):
    def setUp(self):
        self.work_dir = tempfile.mkdtemp(prefix="extdedup-test-")
        self.addCleanup(shutil.rmtree, self.work_dir, True)

    def run_operator(self, keys, limit, **kwargs):
        kwargs.setdefault("work_dir", self.work_dir)
        op = ExternalDedup(limit, **kwargs)
        for key in keys:
            op.add(key)
        result = list(op)
        stats = (op.spill_count, op.spill_bytes)
        op.close()
        return result, stats

    # ---------------------------------------------------------- scenarios

    def test_all_fit_in_memory(self):
        keys = make_keys(20_000, 30_000, seed=1)
        result, (spills, _) = self.run_operator(keys, 64 << 20)
        self.assertEqual(spills, 0, "no spill file may be created when data fits")
        self.assertEqual(result, sorted(set(keys)))
        self.assertEqual(os.listdir(self.work_dir), [], "run dir must be cleaned")

    def test_exact_spill_threshold(self):
        """'Just triggers spill': K keys fit, K+1 keys force exactly one spill."""
        keys = make_keys(4_000, 4_000, seed=2)  # all distinct
        limit = 256 << 10

        # Probe: find K = number of keys that fit before the first spill.
        op = ExternalDedup(limit, work_dir=self.work_dir)
        trigger_index = None
        for index, key in enumerate(keys):
            op.add(key)
            if op.spill_count:
                trigger_index = index
                break
        op.close()
        self.assertIsNotNone(trigger_index, "limit too large, no spill observed")
        fit_count = trigger_index  # keys[0:trigger_index] fit without spilling

        # Exactly-at-threshold: no spill at all.
        result, (spills, _) = self.run_operator(keys[:fit_count], limit)
        self.assertEqual(spills, 0)
        self.assertEqual(result, sorted(set(keys[:fit_count])))

        # One key over the threshold: exactly one spill, result still exact.
        result, (spills, _) = self.run_operator(keys[: fit_count + 1], limit)
        self.assertEqual(spills, 1)
        self.assertEqual(result, sorted(set(keys[: fit_count + 1])))
        print(
            f"\n[exact-spill] limit={limit}B fits {fit_count} keys; "
            f"{fit_count}+1 keys -> exactly 1 spill, result identical"
        )

    def test_repeated_spills_and_multipass_merge(self):
        keys = make_keys(120_000, 90_000, seed=3)
        # Small limit -> many spills; max_open_runs=8 -> multi-pass merging.
        result, (spills, spill_bytes) = self.run_operator(
            keys, 256 << 10, max_open_runs=8
        )
        self.assertGreaterEqual(spills, 5, "expected repeated spilling")
        self.assertEqual(result, sorted(set(keys)))
        print(
            f"\n[repeated-spill] n={len(keys)} spills={spills} "
            f"spill_bytes={spill_bytes} match=True"
        )

    def test_all_keys_identical(self):
        keys = [b"the-same-key"] * 500_000
        result, (spills, _) = self.run_operator(keys, 1 << 20)
        self.assertEqual(spills, 0, "a single unique key must never spill")
        self.assertEqual(result, [b"the-same-key"])

    def test_empty_input(self):
        result, (spills, _) = self.run_operator([], 1 << 20)
        self.assertEqual((result, spills), ([], 0))

    # ------------------------------------------------------ hash collisions

    def test_hash_collision_never_causes_false_dedup(self):
        rng = random.Random(7)
        universe = [f"value-{i:04d}" for i in range(300)]
        values = [rng.choice(universe) for _ in range(5_000)]
        keys = [CollidingKey(v) for v in values]
        self.assertEqual(
            len({hash(k) for k in keys}), 1, "precondition: all hashes collide"
        )
        # Force the spill+merge path with a tiny limit.
        result, (spills, _) = self.run_operator(keys, 4 << 10, **COLLIDING_KWARGS)
        self.assertGreater(spills, 0)
        got = [k.v for k in result]
        want = sorted(set(values))
        self.assertEqual(got, want)
        self.assertEqual(len(got), len(set(values)))
        print(
            f"\n[collision] {len(keys)} keys, all hash()=={hash(keys[0])}, "
            f"spills={spills}, distinct={len(want)} -> no false dedup"
        )

    # ------------------------------------------------------- differential

    def test_differential_against_in_memory_set(self):
        """对拍: spilled result must equal the unspilled in-memory result."""
        cases = [
            # seed, n, universe, limit, max_open_runs
            (11, 50_000, 60_000, 64 << 20, 128),   # fits entirely
            (12, 50_000, 40_000, 1 << 20, 128),    # a few spills
            (13, 200_000, 150_000, 1 << 20, 128),  # many spills
            (14, 100_000, 1, 1 << 20, 128),        # all identical
            (15, 120_000, 90_000, 512 << 10, 8),   # repeated spills + multipass
            (16, 80_000, 80_000, 768 << 10, 16),   # mostly unique, forced spill
        ]
        print("\n[diff] seed | n | universe | limit | spills | spill_bytes | unique | match")
        for seed, n, universe, limit, fan in cases:
            keys = make_keys(n, universe, seed)
            result, (spills, spill_bytes) = self.run_operator(
                keys, limit, max_open_runs=fan
            )
            want = sorted(set(keys))
            self.assertEqual(result, want, f"seed={seed}")
            print(
                f"[diff] {seed:4d} | {n:7d} | {universe:8d} | {limit:9d} | "
                f"{spills:6d} | {spill_bytes:11d} | {len(want):6d} | True"
            )

    # ------------------------------------------------------------- cleanup

    def test_cleanup_and_stale_dir_isolation(self):
        # 1. Normal run cleans up after itself.
        keys = make_keys(30_000, 25_000, seed=21)
        result, (spills, _) = self.run_operator(keys, 128 << 10)
        self.assertGreater(spills, 0)
        self.assertEqual(os.listdir(self.work_dir), [])

        # 2. A leftover directory from a 'killed' run must not affect results.
        stale = os.path.join(self.work_dir, "extdedup-stale-000123")
        os.makedirs(stale)
        with open(os.path.join(stale, "run-000000.tmp"), "wb") as fh:
            fh.write(b"\x00\x00\x00\x05garbage-record-from-a-crashed-run")
        result2, _ = self.run_operator(keys, 128 << 10)
        self.assertEqual(result2, sorted(set(keys)), "stale files must be inert")
        self.assertTrue(os.path.isdir(stale), "foreign stale dir must be untouched")

        # 3. sweep_stale_run_dirs reclaims the leftover.
        removed = sweep_stale_run_dirs(self.work_dir)
        self.assertIn(stale, removed)
        self.assertEqual(os.listdir(self.work_dir), [])

    def test_convenience_wrapper(self):
        keys = make_keys(10_000, 8_000, seed=31)
        got = list(dedup(iter(keys), 256 << 10, work_dir=self.work_dir))
        self.assertEqual(got, sorted(set(keys)))




CHILD_SCRIPT = r"""
import os, sys, time
sys.path.insert(0, os.getcwd())
from extdedup import ExternalDedup
work_dir = sys.argv[1]
op = ExternalDedup(64 << 10, work_dir=work_dir)
print(op.run_dir, flush=True)          # tell the parent where the spills live
i = 0
while True:                            # keep spilling until killed
    op.add(("key-%08d" % i).encode())
    i += 1
    if i % 2000 == 0:
        time.sleep(0.001)
"""


class TestKillSafety(unittest.TestCase):
    def test_sigterm_mid_spill_leaves_no_residue(self):
        import subprocess

        work_dir = tempfile.mkdtemp(prefix="extdedup-kill-")
        self.addCleanup(shutil.rmtree, work_dir, True)
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD_SCRIPT, work_dir],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        try:
            run_dir = child.stdout.readline().strip()
            self.assertTrue(run_dir.startswith(work_dir))
            # Wait until at least one spill file exists, then kill.
            deadline = time.time() + 30
            while time.time() < deadline:
                if os.path.isdir(run_dir) and os.listdir(run_dir):
                    break
                time.sleep(0.01)
            self.assertTrue(os.listdir(run_dir), "child never spilled")
            child.terminate()  # SIGTERM mid-run
            child.wait(timeout=30)
            self.assertNotEqual(child.returncode, 0)
            self.assertFalse(
                os.path.exists(run_dir),
                "SIGTERM cleanup must remove the run directory",
            )
            self.assertEqual(os.listdir(work_dir), [])
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()

        # A fresh run in the same directory is unaffected either way.
        keys = make_keys(5_000, 4_000, seed=41)
        op = ExternalDedup(64 << 10, work_dir=work_dir)
        for key in keys:
            op.add(key)
        self.assertEqual(list(op), sorted(set(keys)))
        op.close()


import subprocess  # noqa: E402  (used by TestKillSafety and CHILD_SCRIPT env)
import time  # noqa: E402


if __name__ == "__main__":
    unittest.main(verbosity=2)

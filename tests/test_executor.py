import json
import os
import sqlite3
import tempfile
import unittest

from sqlchunk.executor import (
    ChunkExecutor,
    CrashInjected,
    RetryPolicy,
    RetryableError,
    make_sqlite_executor,
)

SQL = (
    b"CREATE TABLE t(a TEXT, b INTEGER); "
    b"INSERT INTO t VALUES('x', 1); "
    b"INSERT INTO t VALUES('y', 2); "
    b"INSERT INTO t VALUES('z', 3);"
)


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.ckpt = os.path.join(self.dir, "t.ckpt")

    def make(self, chunk_size=60, crash_hook=None, policy=None, run_chunk=None):
        if run_chunk is None:
            run_chunk, self.conn = make_sqlite_executor(self.db)
        else:
            self.conn = sqlite3.connect(self.db)
        return ChunkExecutor(
            SQL,
            self.ckpt,
            run_chunk,
            chunk_size=chunk_size,
            policy=policy or RetryPolicy(max_retries=3),
            crash_hook=crash_hook,
            sleep=lambda _s: None,
        )

    def rows(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT a, b FROM t ORDER BY b").fetchall()
        finally:
            conn.close()

    def test_full_run_single_chunk(self):
        summary = self.make(chunk_size=1 << 20).run()
        self.assertEqual(summary.status, "done")
        self.assertEqual(summary.chunks_total, 1)
        self.assertEqual(self.rows(), [("x", 1), ("y", 2), ("z", 3)])

    def test_crash_and_resume(self):
        calls = {"n": 0}

        def hook(_i):
            calls["n"] += 1
            if calls["n"] == 2:
                raise CrashInjected()

        with self.assertRaises(CrashInjected):
            self.make(crash_hook=hook).run()
        self.conn.close()
        data = json.load(open(self.ckpt))
        statuses = [c["status"] for c in data["chunks"]]
        self.assertEqual(statuses[:2], ["done", "done"])
        self.assertTrue(all(s == "pending" for s in statuses[2:]))

        summary = self.make().run()
        self.assertEqual(summary.status, "done")
        self.assertEqual(self.rows(), [("x", 1), ("y", 2), ("z", 3)])
        # resumed run must not have re-executed done chunks
        data = json.load(open(self.ckpt))
        self.assertTrue(all(c["attempts"] == 1 for c in data["chunks"]))

    def test_rerun_after_completion_is_noop(self):
        self.make().run()
        self.conn.close()
        summary = self.make().run()
        self.assertEqual(summary.status, "done")
        self.assertEqual(summary.results, [])
        self.assertEqual(self.rows(), [("x", 1), ("y", 2), ("z", 3)])

    def test_fatal_error_not_retried(self):
        bad = SQL + b" THIS IS NOT SQL;"
        run_chunk, conn = make_sqlite_executor(self.db)
        ex = ChunkExecutor(bad, self.ckpt, run_chunk, chunk_size=60,
                           policy=RetryPolicy(max_retries=3),
                           sleep=lambda _s: None)
        summary = ex.run()
        self.assertEqual(summary.status, "failed_fatal")
        data = json.load(open(self.ckpt))
        last = data["chunks"][-1]
        self.assertEqual(last["status"], "failed_fatal")
        self.assertEqual(last["attempts"], 1)
        self.assertEqual(last["retries"], 0)
        self.assertIn("syntax error", last["error_message"])
        # rerun surfaces the recorded fatal error without re-executing
        summary2 = ex.run()
        self.assertEqual(summary2.status, "failed_fatal")
        conn.close()

    def test_retryable_error_retries_and_recovers(self):
        attempts = {"n": 0}
        real_run, conn = make_sqlite_executor(self.db)

        def flaky(chunk):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise RetryableError("simulated transient failure")
            real_run(chunk)

        ex = ChunkExecutor(SQL, self.ckpt, flaky, chunk_size=1 << 20,
                           policy=RetryPolicy(max_retries=3),
                           sleep=lambda _s: None)
        summary = ex.run()
        self.assertEqual(summary.status, "done")
        self.assertEqual(attempts["n"], 3)
        data = json.load(open(self.ckpt))
        self.assertEqual(data["chunks"][0]["attempts"], 3)
        self.assertEqual(data["chunks"][0]["retries"], 2)
        conn.close()

    def test_retryable_exhaustion_records_final_state(self):
        def always_fail(chunk):
            raise RetryableError("permanent-transient failure")

        ex = ChunkExecutor(SQL, self.ckpt, always_fail, chunk_size=1 << 20,
                           policy=RetryPolicy(max_retries=3),
                           sleep=lambda _s: None)
        summary = ex.run()
        self.assertEqual(summary.status, "failed_retryable")
        data = json.load(open(self.ckpt))
        rec = data["chunks"][0]
        self.assertEqual(rec["status"], "failed_retryable")
        self.assertEqual(rec["attempts"], 4)  # 1 initial + 3 retries
        self.assertEqual(rec["retries"], 3)
        self.assertEqual(rec["error_type"], "RetryableError")

    def test_failed_chunk_rolls_back_partial_effects(self):
        # chunk contains a good INSERT then a bad statement: nothing commits
        bad = (
            b"CREATE TABLE t(a TEXT, b INTEGER); "
            b"INSERT INTO t VALUES('x', 1); BROKEN SQL HERE; "
            b"INSERT INTO t VALUES('y', 2);"
        )
        run_chunk, conn = make_sqlite_executor(self.db)
        ex = ChunkExecutor(bad, self.ckpt, run_chunk, chunk_size=45,
                           policy=RetryPolicy(max_retries=0),
                           sleep=lambda _s: None)
        summary = ex.run()
        self.assertEqual(summary.status, "failed_fatal")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)
        conn.close()

    def test_sqlite_lock_is_retryable(self):
        policy = RetryPolicy()
        self.assertTrue(policy.retryable_check(
            sqlite3.OperationalError("database is locked")))
        self.assertFalse(policy.retryable_check(
            sqlite3.OperationalError('near "X": syntax error')))
        self.assertFalse(policy.retryable_check(
            sqlite3.IntegrityError("UNIQUE constraint failed")))


if __name__ == "__main__":
    unittest.main()

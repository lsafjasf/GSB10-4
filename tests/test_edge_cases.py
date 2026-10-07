"""Boundary cases required by the spec:

* single chunk
* oversized single statement chunk
* script syntax error (non-retryable, no retry)
* corrupted checkpoint (garbage / truncation / script mismatch / tampering)
* unterminated last statement
* commit-before-checkpoint crash window (idempotent replay safety)
"""

import json
import os
import sqlite3
import tempfile
import unittest

from sqlchunk.checkpoint import CheckpointCorrupted, CheckpointStore
from sqlchunk.executor import (
    ChunkExecutor,
    CrashInjected,
    RetryPolicy,
    make_sqlite_executor,
)


class EdgeCaseTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def exec_sql(self, sql, size, hook=None, ckpt="r.ckpt", retries=3):
        db = os.path.join(self.dir, "t.db")
        run_chunk, conn = make_sqlite_executor(db)
        ex = ChunkExecutor(
            sql,
            os.path.join(self.dir, ckpt),
            run_chunk,
            chunk_size=size,
            crash_hook=hook,
            policy=RetryPolicy(max_retries=retries),
            sleep=lambda _s: None,
        )
        return ex.run(), conn

    def test_single_chunk(self):
        sql = b"CREATE TABLE t(a INT); INSERT INTO t VALUES(1);"
        summary, conn = self.exec_sql(sql, 1 << 20)
        self.assertEqual(summary.status, "done")
        self.assertEqual(summary.chunks_total, 1)
        self.assertEqual(summary.chunks_done, 1)
        conn.close()

    def test_oversized_chunk_executes(self):
        big = b"INSERT INTO t VALUES('" + b"z" * 4000 + b"');"
        sql = b"CREATE TABLE t(a TEXT); " + big + b" INSERT INTO t VALUES('s');"
        summary, conn = self.exec_sql(sql, 256)
        self.assertEqual(summary.status, "done")
        over = [r for r in summary.results if r.index == 1][0]
        self.assertEqual(over.status, "done")
        data = json.load(open(os.path.join(self.dir, "r.ckpt")))
        self.assertTrue(data["chunks"][1]["oversized"])
        self.assertGreater(data["chunks"][1]["bytes"], 256)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 2)
        conn.close()

    def test_script_syntax_error_is_fatal(self):
        sql = b"CREATE TABLE t(a INT); THIS IS BROKEN SQL; INSERT INTO t VALUES(1);"
        summary, conn = self.exec_sql(sql, 1 << 20, retries=5)
        self.assertEqual(summary.status, "failed_fatal")
        rec = [r for r in summary.results if r.status == "failed_fatal"][0]
        self.assertEqual(rec.attempts, 1)
        self.assertEqual(rec.retries, 0)
        self.assertEqual(rec.error_type, "OperationalError")
        conn.close()

    def test_checkpoint_garbage(self):
        ckpt = os.path.join(self.dir, "bad.ckpt")
        with open(ckpt, "wb") as fh:
            fh.write(b"\x00\x01not-json\xff\xfe")
        with self.assertRaises(CheckpointCorrupted):
            CheckpointStore.load(ckpt, b"SELECT 1;", [])

    def test_checkpoint_truncated(self):
        sql = b"CREATE TABLE t(a INT); INSERT INTO t VALUES(1);"
        path = os.path.join(self.dir, "r.ckpt")
        summary, conn = self.exec_sql(sql, 30)
        conn.close()
        self.assertEqual(summary.status, "done")
        raw = open(path, "rb").read()
        with open(path, "wb") as fh:
            fh.write(raw[: len(raw) // 2])
        with self.assertRaises(CheckpointCorrupted):
            self.exec_sql(sql, 30)

    def test_checkpoint_script_mismatch(self):
        sql = b"CREATE TABLE t(a INT); INSERT INTO t VALUES(1);"
        summary, conn = self.exec_sql(sql, 30, ckpt="m.ckpt")
        conn.close()
        different = b"CREATE TABLE t(a INT); INSERT INTO t VALUES(2);"
        run_chunk, conn2 = make_sqlite_executor(os.path.join(self.dir, "t2.db"))
        ex = ChunkExecutor(different, os.path.join(self.dir, "m.ckpt"),
                           run_chunk, chunk_size=30)
        with self.assertRaises(CheckpointCorrupted):
            ex.run()
        conn2.close()

    def test_checkpoint_tampered_sha(self):
        sql = b"CREATE TABLE t(a INT); INSERT INTO t VALUES(1); INSERT INTO t VALUES(2);"
        summary, conn = self.exec_sql(sql, 30, ckpt="x.ckpt")
        conn.close()
        path = os.path.join(self.dir, "x.ckpt")
        data = json.load(open(path))
        data["chunks"][0]["sha256"] = "a" * 64
        json.dump(data, open(path, "w"))
        with self.assertRaises(CheckpointCorrupted):
            self.exec_sql(sql, 30, ckpt="x.ckpt")

    def test_unterminated_last_statement(self):
        sql = b"CREATE TABLE t(a INT); INSERT INTO t VALUES(1)"  # no terminator
        summary, conn = self.exec_sql(sql, 1 << 20)
        self.assertEqual(summary.status, "done")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)
        conn.close()

    def test_commit_before_checkpoint_window_idempotent_replay(self):
        # Crash injected *after* the chunk's COMMIT but before the caller
        # sees success. With idempotent statements (INSERT OR REPLACE on a
        # primary key) the resume produces the same final state as baseline.
        sql = b"".join(
            b"INSERT OR REPLACE INTO t VALUES(%d, 'v%d');" % (i, i)
            for i in range(60)
        )
        sql = b"CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT); " + sql
        path = os.path.join(self.dir, "w.ckpt")

        real_run, conn = make_sqlite_executor(os.path.join(self.dir, "w.db"))
        fired = {"done": False}

        def run_with_post_commit_crash(chunk):
            real_run(chunk)  # COMMIT has happened here
            if chunk.index == 1 and not fired["done"]:
                fired["done"] = True
                raise CrashInjected("commit done, checkpoint not yet written")

        ex = ChunkExecutor(sql, path, run_with_post_commit_crash,
                           chunk_size=300, sleep=lambda _s: None)
        with self.assertRaises(CrashInjected):
            ex.run()

        run2, conn2 = make_sqlite_executor(os.path.join(self.dir, "w.db"))
        summary = ChunkExecutor(sql, path, run2, chunk_size=300,
                                sleep=lambda _s: None).run()
        self.assertEqual(summary.status, "done")
        self.assertEqual(conn2.execute("SELECT COUNT(*) FROM t").fetchone()[0], 60)
        self.assertEqual(
            conn2.execute("SELECT MIN(id), MAX(id), SUM(id) FROM t").fetchone(),
            (0, 59, 59 * 60 // 2))
        conn.close()
        conn2.close()


if __name__ == "__main__":
    unittest.main()

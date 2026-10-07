"""Differential test: chunked+resumed execution equals one-shot execution.

Artifacts are written to testdata/:

* diff_script.sql     -- the stress script (semicolons hidden in strings,
                         comments, quotes, DELIMITER blocks, ...)
* diff_oneshot.sql    -- full sqlite3 .dump of the one-shot run
* diff_chunked.sql    -- full sqlite3 .dump of the chunked+crash run
* diff_summary.json   -- chunk/checkpoint comparison metadata

The comparison is performed both on the raw dump and on normalized
per-table contents.
"""

import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "testdata")
sys.path.insert(0, ROOT)

from sqlchunk.executor import (  # noqa: E402
    ChunkExecutor,
    CrashInjected,
    make_sqlite_executor,
)
from sqlchunk.lexer import split_statements  # noqa: E402


def build_script() -> bytes:
    """A script whose semicolons appear inside every protected construct."""
    parts = [
        "-- generated differential script; semicolon; in header comment\n",
        "CREATE TABLE accounts(id INTEGER PRIMARY KEY, name TEXT, balance INTEGER);\n",
        "/* block ; comment with ; multiple */ CREATE TABLE log(id INTEGER PRIMARY KEY, msg TEXT);\n",
    ]
    rng = random.Random(42)
    raw_names = [
        "al;ice", "bo;b;;o", "car'ol;o", "di;no'x", "eve;y;thing",
        "frank;z", "gr;ace", "h;e;idi", "iva;n'p", "jud;y",
    ]
    for i in range(60):
        name = raw_names[i % len(raw_names)].replace("'", "''")
        parts.append(
            "INSERT OR REPLACE INTO accounts VALUES(%d, '%s', %d);\n"
            % (i, name, rng.randint(0, 9999))
        )
        if i % 7 == 0:
            parts.append("-- log; the %d-th row\n" % i)
            parts.append(
                "INSERT OR REPLACE INTO log VALUES(%d, 'step;%d;done');\n" % (i, i)
            )
        if i % 13 == 0:
            parts.append("/* audit; checkpoint; row %d */\n" % i)
    # DELIMITER block: semicolons inside a trigger body must not split it.
    parts.append(
        "DELIMITER $$\n"
        "CREATE TRIGGER accounts_ai AFTER INSERT ON accounts BEGIN\n"
        "  INSERT OR REPLACE INTO log VALUES(NEW.id, 'trigger;' || NEW.name || ';end');\n"
        "END$$\n"
        "DELIMITER ;\n"
    )
    for i in range(60, 80):
        parts.append(
            "INSERT OR REPLACE INTO accounts VALUES(%d, 'post%d;name', %d);\n"
            % (i, i, rng.randint(0, 9999))
        )
    parts.append(
        "CREATE VIEW rich AS SELECT id, name FROM accounts WHERE balance > 9000;\n"
        "CREATE INDEX idx_balance ON accounts(balance);\n"
    )
    return "".join(parts).encode("utf-8")


def statement_by_statement(db_path: str, script: bytes):
    """Baseline: same splitter, statements executed one by one, no chunking."""
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    for stmt in split_statements(script):
        if not stmt.has_code:
            continue
        conn.execute("BEGIN")
        conn.execute(stmt.text.decode("utf-8"))
        conn.execute("COMMIT")
    return conn


def chunked_with_crashes(db_path: str, ckpt_path: str, script: bytes,
                         chunk_size: int):
    """Chunked run with hard crashes after fixed chunk indexes."""
    crash_after = {1, 4}
    fired = set()

    def run(chunk):
        real(chunk)
        if chunk.index in crash_after and chunk.index not in fired:
            fired.add(chunk.index)
            raise CrashInjected()

    real, conn = make_sqlite_executor(db_path)
    ex = ChunkExecutor(script, ckpt_path, run, chunk_size=chunk_size,
                       sleep=lambda _s: None)
    for _ in range(10):
        try:
            return ex.run(), conn
        except CrashInjected:
            continue
    raise AssertionError("resume loop did not converge")


def dump_db(db_path: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c",
         "import sqlite3,sys;"
         "con=sqlite3.connect(sys.argv[1]);"
         "print('\\n'.join(con.iterdump()))", db_path],
        check=True, capture_output=True, text=True,
    ).stdout
    return out


def normalized_tables(db_path: str):
    conn = sqlite3.connect(db_path)
    result = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
        "ORDER BY name"
    ):
        try:
            rows = conn.execute(
                'SELECT * FROM "%s" ORDER BY 1,2,3' % name.replace('"', '""')
            ).fetchall()
        except sqlite3.Error:
            continue
        result[name] = rows
    conn.close()
    return result


class DifferentialTest(unittest.TestCase):
    def test_chunked_equals_oneshot(self):
        script = build_script()
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "diff_script.sql"), "wb") as fh:
            fh.write(script)
        tmp = tempfile.mkdtemp()
        db_one = os.path.join(tmp, "one.db")
        db_chunk = os.path.join(tmp, "chunk.db")
        ckpt = os.path.join(tmp, "run.ckpt")

        statement_by_statement(db_one, script)
        summary, conn_chunk = chunked_with_crashes(
            db_chunk, ckpt, script, chunk_size=700
        )
        self.assertEqual(summary.status, "done")
        conn_chunk.close()

        dump_one = dump_db(db_one)
        dump_chunk = dump_db(db_chunk)
        with open(os.path.join(DATA_DIR, "diff_oneshot.sql"), "w") as fh:
            fh.write(dump_one)
        with open(os.path.join(DATA_DIR, "diff_chunked.sql"), "w") as fh:
            fh.write(dump_chunk)

        self.assertEqual(dump_one, dump_chunk,
                         "raw sqlite3 dumps differ between one-shot and chunked")
        self.assertEqual(normalized_tables(db_one),
                         normalized_tables(db_chunk))

        ck = json.load(open(ckpt))
        chunk_rows = ck["chunks"]
        summary_json = {
            "script_bytes": len(script),
            "statement_total": ck["statement_total"],
            "chunk_total": ck["chunk_total"],
            "chunk_size_bytes": ck["chunk_size"],
            "chunks": [
                {
                    "index": c["index"],
                    "statements": c["statements"],
                    "bytes": c["bytes"],
                    "status": c["status"],
                    "attempts": c["attempts"],
                }
                for c in chunk_rows
            ],
            "crashed_after_chunks": [1, 4],
            "dumps_identical": dump_one == dump_chunk,
        }
        with open(os.path.join(DATA_DIR, "diff_summary.json"), "w") as fh:
            json.dump(summary_json, fh, indent=2, ensure_ascii=False)

        self.assertEqual(summary_json["statement_total"],
                         len(split_statements(script)))
        self.assertTrue(all(c["status"] == "done" for c in chunk_rows))
        self.assertGreaterEqual(chunk_rows[2]["attempts"], 1)


if __name__ == "__main__":
    unittest.main()

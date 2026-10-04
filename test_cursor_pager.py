"""Self-tests for cursor tampering, ties, and concurrent insert/delete."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest

from cursor_pager import (
    CursorError,
    QuerySpec,
    decode_cursor,
    drain,
    encode_cursor,
    fetch_page,
)

SECRET = b"unit-test-secret-do-not-use-in-prod"


def make_db(path: str | None = None, rows: int = 0, ties: int = 1) -> sqlite3.Connection:
    conn = sqlite3.connect(path or ":memory:")
    conn.execute(
        "CREATE TABLE items ("
        "id INTEGER PRIMARY KEY, k INTEGER NOT NULL, label TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX idx_items_k_id ON items(k, id)")
    if rows:
        batch = [
            (i, i // ties, f"row-{i}")
            for i in range(rows)
        ]
        conn.executemany("INSERT INTO items(id, k, label) VALUES(?,?,?)", batch)
        conn.commit()
    return conn


SPEC = QuerySpec(table="items", sort_col="k", id_col="id")
SPEC_TIED = QuerySpec(table="items", sort_col="k", id_col="id")


class BasicPaginationTests(unittest.TestCase):
    def test_first_middle_last_and_empty(self):
        conn = make_db(rows=25)
        first = fetch_page(conn, SPEC, 10, SECRET)
        self.assertEqual([r["id"] for r in first.rows], list(range(10)))
        self.assertTrue(first.has_more)
        self.assertIsNotNone(first.next_cursor)

        second = fetch_page(conn, SPEC, 10, SECRET, first.next_cursor)
        self.assertEqual([r["id"] for r in second.rows], list(range(10, 20)))

        third = fetch_page(conn, SPEC, 10, SECRET, second.next_cursor)
        self.assertEqual([r["id"] for r in third.rows], list(range(20, 25)))
        self.assertFalse(third.has_more)
        self.assertIsNone(third.next_cursor)

    def test_exact_page_boundary(self):
        conn = make_db(rows=20)
        p1 = fetch_page(conn, SPEC, 10, SECRET)
        self.assertTrue(p1.has_more)
        p2 = fetch_page(conn, SPEC, 10, SECRET, p1.next_cursor)
        self.assertEqual(len(p2.rows), 10)
        self.assertFalse(p2.has_more)
        self.assertIsNone(p2.next_cursor)
        # next_cursor=None is the terminal signal: callers must stop here.
        # (Passing None again would restart at the first page by design.)

    def test_empty_result(self):
        conn = make_db(rows=0)
        page = fetch_page(conn, SPEC, 10, SECRET)
        self.assertEqual(page.rows, [])
        self.assertFalse(page.has_more)
        self.assertIsNone(page.next_cursor)

    def test_page_bigger_than_table(self):
        conn = make_db(rows=3)
        page = fetch_page(conn, SPEC, 100, SECRET)
        self.assertEqual(len(page.rows), 3)
        self.assertIsNone(page.next_cursor)

    def test_filtered_empty_result(self):
        conn = make_db(rows=10)
        spec = QuerySpec("items", "k", "id", where_sql="k > ?", params=(1000,))
        page = fetch_page(conn, spec, 5, SECRET)
        self.assertEqual(page.rows, [])
        self.assertIsNone(page.next_cursor)

    def test_drain_matches_full_scan(self):
        conn = make_db(rows=503, ties=7)
        paged = drain(conn, SPEC_TIED, 50, SECRET)
        full = conn.execute(
            "SELECT * FROM items ORDER BY k ASC, id ASC"
        ).fetchall()
        self.assertEqual([r["id"] for r in paged], [r[0] for r in full])

    def test_descending_order(self):
        conn = make_db(rows=15)
        spec = QuerySpec("items", "k", "id", descending=True)
        page = fetch_page(conn, spec, 6, SECRET)
        self.assertEqual([r["id"] for r in page.rows], list(range(14, 8, -1)))
        page2 = fetch_page(conn, spec, 6, SECRET, page.next_cursor)
        self.assertEqual([r["id"] for r in page2.rows], list(range(8, 2, -1)))


class StableTiebreakerTests(unittest.TestCase):
    def test_ties_stable_across_pages(self):
        # 1000 distinct rows but only 10 distinct sort values => huge tie runs;
        # id is the stable secondary key, so the boundary must not wobble.
        conn = make_db(rows=1000, ties=100)
        seen: list[int] = []
        token = None
        for _ in range(40):
            page = fetch_page(conn, SPEC_TIED, 37, SECRET, token)
            ids = [r["id"] for r in page.rows]
            self.assertEqual(len(ids), len(set(ids)), "duplicate id in a page")
            seen.extend(ids)
            token = page.next_cursor
            if token is None:
                break
        self.assertEqual(len(seen), 1000)
        self.assertEqual(len(set(seen)), 1000)
        self.assertEqual(seen, list(range(1000)))

    def test_tiebreak_compare_is_composite(self):
        # Equal k must continue by id, never re-emit a same-k row from the
        # previous page or skip the remaining same-k rows.
        conn = make_db(rows=10, ties=10)  # all rows have k == 0
        p1 = fetch_page(conn, SPEC, 4, SECRET)
        p2 = fetch_page(conn, SPEC, 4, SECRET, p1.next_cursor)
        p3 = fetch_page(conn, SPEC, 4, SECRET, p2.next_cursor)
        ids = [r["id"] for r in p1.rows + p2.rows + p3.rows]
        self.assertEqual(ids, list(range(10)))


class CursorTamperTests(unittest.TestCase):
    def setUp(self):
        self.token = encode_cursor(42, 7, SPEC.fingerprint(), SECRET)

    def test_roundtrip(self):
        k, i = decode_cursor(self.token, SPEC.fingerprint(), SECRET)
        self.assertEqual((k, i), (42, 7))

    def test_flip_payload_char_rejected(self):
        v, body, tag = self.token.split(".")
        flipped = body[:-2] + ("A" if body[-2] != "A" else "B") + body[-1:]
        with self.assertRaises(CursorError):
            decode_cursor(f"{v}.{flipped}.{tag}", SPEC.fingerprint(), SECRET)

    def test_flip_tag_char_rejected(self):
        v, body, tag = self.token.split(".")
        bad = tag[:-1] + ("A" if tag[-1] != "A" else "B")
        with self.assertRaises(CursorError):
            decode_cursor(f"{v}.{body}.{bad}", SPEC.fingerprint(), SECRET)

    def test_truncated_token_rejected(self):
        for cut in (1, 2, 3, 10, len(self.token) - 1):
            with self.assertRaises(CursorError):
                decode_cursor(self.token[:cut], SPEC.fingerprint(), SECRET)

    def test_extra_segment_rejected(self):
        with self.assertRaises(CursorError):
            decode_cursor(self.token + ".extra", SPEC.fingerprint(), SECRET)

    def test_bad_version_rejected(self):
        _, body, tag = self.token.split(".")
        with self.assertRaises(CursorError):
            decode_cursor(f"v2.{body}.{tag}", SPEC.fingerprint(), SECRET)

    def test_non_base64_rejected(self):
        _, body, tag = self.token.split(".")
        with self.assertRaises(CursorError):
            decode_cursor(f"v1.{body}!!!.{tag}", SPEC.fingerprint(), SECRET)

    def test_payload_not_json_rejected(self):
        _, body, tag = self.token.split(".")
        # Build a structurally valid token whose payload is not JSON; the tag
        # must cover the body, so recompute it with a *different* secret to
        # prove the HMAC -- not JSON parsing -- rejects attacker input first.
        import base64
        import hashlib
        import hmac

        evil_body = base64.urlsafe_b64encode(b"not-json").rstrip(b"=").decode()
        with self.assertRaises(CursorError):
            decode_cursor(f"v1.{evil_body}.{tag}", SPEC.fingerprint(), SECRET)
        # And even with a valid tag under the shared secret, payload shape is
        # checked after signature verification.
        evil_tag = hmac.new(
            SECRET, f"v1.{evil_body}".encode(), hashlib.sha256
        ).digest()
        evil_tag_s = base64.urlsafe_b64encode(evil_tag).rstrip(b"=").decode()
        with self.assertRaises(CursorError):
            decode_cursor(
                f"v1.{evil_body}.{evil_tag_s}", SPEC.fingerprint(), SECRET
            )

    def test_wrong_secret_rejected(self):
        with self.assertRaises(CursorError):
            decode_cursor(self.token, SPEC.fingerprint(), b"another-secret")

    def test_cross_query_cursor_rejected(self):
        other = QuerySpec(
            "items", "k", "id", where_sql="k > ?", params=(0,)
        )
        with self.assertRaises(CursorError):
            fetch_page(make_db(rows=5), other, 2, SECRET, self.token)

    def test_cross_filter_params_rejected(self):
        spec_a = QuerySpec("items", "k", "id", where_sql="label LIKE ?",
                           params=("row-%",))
        spec_b = QuerySpec("items", "k", "id", where_sql="label LIKE ?",
                           params=("zzz%",))
        conn = make_db(rows=5)
        page = fetch_page(conn, spec_a, 2, SECRET)
        with self.assertRaises(CursorError):
            fetch_page(conn, spec_b, 2, SECRET, page.next_cursor)

    def test_non_string_and_empty_rejected(self):
        for bad in (None, "", 123, b"v1.x.y", [], object()):
            with self.assertRaises(CursorError):
                decode_cursor(bad, SPEC.fingerprint(), SECRET)


class ConcurrentMutationTests(unittest.TestCase):
    def _walk(self, conn, mutate):
        seen_ids: list[int] = []
        token = None
        mutate(seen_ids, token, before_first=True)
        while True:
            page = fetch_page(conn, SPEC, 10, SECRET, token)
            ids = [r["id"] for r in page.rows]
            self.assertEqual(len(ids), len(set(ids)), "duplicate inside page")
            seen_ids.extend(ids)
            token = page.next_cursor
            mutate(seen_ids, token)
            if token is None:
                break
        return seen_ids

    def test_delete_during_scan(self):
        conn = make_db(rows=55)
        state = {"deleted_behind": 0, "deleted_ahead": 0}

        def mutate(seen, token, before_first=False):
            if before_first:
                return
            if len(seen) == 10:
                # rows already delivered: disappear from later snapshots,
                # but must not be re-delivered (no-dup guarantee).
                conn.execute("DELETE FROM items WHERE id IN (0,1,2)")
                state["deleted_behind"] = 3
            elif len(seen) == 30:
                # rows not yet reached: simply skipped, no gap/dup.
                conn.execute("DELETE FROM items WHERE id IN (40,41,42)")
                state["deleted_ahead"] = 3
            conn.commit()

        seen = self._walk(conn, mutate)
        self.assertEqual(len(seen), len(set(seen)), "duplicate across pages")
        survivors = set(range(55)) - {0, 1, 2, 40, 41, 42}
        # Every surviving row seen exactly once; deleted rows behind cursor
        # were delivered before deletion (still exactly once).
        self.assertEqual(set(seen) - {0, 1, 2}, survivors)

    def test_insert_during_scan(self):
        conn = make_db(rows=40)

        def mutate(seen, token, before_first=False):
            if before_first:
                # inserted strictly *behind* the first cursor boundary is only
                # done after page 1: emulate here for determinism.
                return
            if len(seen) == 10:
                # Behind the cursor (small k/id): must NOT be back-filled.
                conn.executemany(
                    "INSERT INTO items(id,k,label) VALUES(?,?,?)",
                    [(1000, -10, "late-behind"), (1001, -5, "late-behind2")],
                )
                # Ahead of the cursor: must be picked up in a later page.
                conn.executemany(
                    "INSERT INTO items(id,k,label) VALUES(?,?,?)",
                    [(1002, 35, "late-ahead"), (1003, 39, "late-ahead2")],
                )
                conn.commit()

        seen = self._walk(conn, mutate)
        self.assertNotIn(1000, seen)
        self.assertNotIn(1001, seen)
        self.assertIn(1002, seen)
        self.assertIn(1003, seen)
        self.assertEqual(len(seen), len(set(seen)))
        # All 40 original rows exactly once.
        self.assertTrue(set(range(40)).issubset(set(seen)))

    def test_concurrent_threaded_mutations(self):
        # Real concurrency: a writer thread hammers inserts/deletes while the
        # reader pages through a file-backed database.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = make_db(path, rows=2000, ties=13)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            stop = threading.Event()

            def writer():
                import time
                wconn = sqlite3.connect(path, timeout=5)
                wconn.execute("PRAGMA busy_timeout=5000")
                next_id = 10_000
                rng_state = 12345
                while not stop.is_set():
                    rng_state = (rng_state * 1103515245 + 12345) & 0x7FFFFFFF
                    if rng_state % 2:
                        k = rng_state % 500
                        wconn.execute(
                            "INSERT OR REPLACE INTO items(id,k,label) "
                            "VALUES(?,?,?)", (next_id, k, "concurrent")
                        )
                        next_id += 1
                    else:
                        wconn.execute(
                            "DELETE FROM items WHERE id = ?",
                            (rng_state % 2000,),
                        )
                    wconn.commit()
                    time.sleep(0.001)
            t = threading.Thread(target=writer, daemon=True)
            t.start()
            try:
                rconn = sqlite3.connect(path, timeout=5)
                rconn.execute("PRAGMA busy_timeout=5000")
                spec = QuerySpec("items", "k", "id")
                seen: list[int] = []
                token = None
                while True:
                    page = fetch_page(rconn, spec, 64, SECRET, token)
                    ids = [r["id"] for r in page.rows]
                    self.assertEqual(len(ids), len(set(ids)))
                    seen.extend(ids)
                    token = page.next_cursor
                    if token is None:
                        break
                # invariant under arbitrary interleaving: never a duplicate
                self.assertEqual(len(seen), len(set(seen)))
            finally:
                stop.set()
                t.join(timeout=5)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)

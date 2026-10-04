"""Self tests: cursor integrity + pagination edge cases under mutation."""
import random
import sqlite3
import time
import unittest

from cursorlib import (
    CursorError,
    decode_cursor,
    encode_cursor,
    fetch_page,
    iter_pages,
)

SECRET = b"unit-test-secret-key-0123456789"
OTHER_SECRET = b"a-completely-different-secret"


def make_db(n, ties=1):
    """n rows; rows share a sort key in groups of `ties` (tie on created_at)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, created_at INTEGER, payload TEXT)"
    )
    rows = [
        (i, 1_700_000_000 + i // ties, f"p{i}")
        for i in range(1, n + 1)
    ]
    conn.executemany("INSERT INTO items (id, created_at, payload) VALUES (?,?,?)", rows)
    conn.commit()
    return conn


def kwargs(cursor=None, limit=100):
    return dict(
        table="items", key_col="created_at", id_col="id",
        secret=SECRET, cursor=cursor, limit=limit,
    )


def full_ids(conn):
    return [r[0] for r in conn.execute(
        "SELECT id FROM items ORDER BY created_at, id")]


class CursorEncodingTests(unittest.TestCase):
    def test_roundtrip_int_float_str(self):
        for key, row_id in [(1, 2), (1.5, "ab"), ("x", 99), (-3, 0)]:
            token = encode_cursor(key, row_id, SECRET)
            self.assertEqual((key, row_id), decode_cursor(token, SECRET))

    def test_tampered_payload_rejected(self):
        token = encode_cursor(10, 20, SECRET)
        _, p, s = token.split(".")
        flipped = ("A" if p[0] != "A" else "B") + p[1:]
        for bad in [f"c1.{flipped}.{s}", "c1." + p + "." + ("A" + s[1:]),
                    "c2." + p + "." + s, "garbage", "", "c1..",
                    "c1." + p[:-1] + "." + s, "c1." + p + "." + s[:-1],
                    "c1." + p + "." + s + "AA"]:
            with self.assertRaises(CursorError):
                decode_cursor(bad, SECRET)

    def test_truncated_at_every_length_rejected(self):
        token = encode_cursor(10, 20, SECRET)
        for cut in range(1, len(token)):
            with self.assertRaises(CursorError):
                decode_cursor(token[:cut], SECRET)

    def test_wrong_secret_rejected(self):
        with self.assertRaises(CursorError):
            decode_cursor(encode_cursor(10, 20, OTHER_SECRET), SECRET)

    def test_non_signed_payload_rejected(self):
        import base64, json
        # Valid JSON, plausible-looking position, but no valid signature.
        payload = json.dumps({"v": 1, "k": 999999, "i": 999999},
                             separators=(",", ":")).encode()
        p = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
        with self.assertRaises(CursorError):
            decode_cursor(f"c1.{p}.AAAAAAAAAAAAAAAAAAAAAA", SECRET)

    def test_schema_violations_rejected(self):
        import base64, hashlib, hmac, json
        # NaN payload (json.dumps refuses it, so hand-craft the bytes)
        raw = b'{"v":1,"k":NaN,"i":2}'
        sig = hmac.new(SECRET, raw, hashlib.sha256).digest()[:16]
        p = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        s = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        with self.assertRaises(CursorError):
            decode_cursor(f"c1.{p}.{s}", SECRET)
        for obj in [
            {"v": 2, "k": 1, "i": 2},          # bad version
            {"v": 1, "k": 1},                  # missing field
            {"v": 1, "k": None, "i": 2},       # null
            {"v": 1, "k": [1], "i": 2},        # array
            {"v": 1, "k": 1, "i": True},       # bool
            "notanobject",
            {"v": 1, "k": 1, "i": 2, "x": 3},  # extra field
        ]:
            raw = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode()
            sig = hmac.new(SECRET, raw, hashlib.sha256).digest()[:16]
            p = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
            s = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
            with self.assertRaises(CursorError):
                decode_cursor(f"c1.{p}.{s}", SECRET)


class PaginationBasicTests(unittest.TestCase):
    def test_empty_result(self):
        conn = make_db(0)
        page = fetch_page(conn, **kwargs())
        self.assertEqual(page.items, [])
        self.assertIsNone(page.next_cursor)

    def test_first_and_last_page_shapes(self):
        conn = make_db(250)
        first = fetch_page(conn, **kwargs(limit=100))
        self.assertEqual([rid for rid, _ in first.items], list(range(1, 101)))
        self.assertIsNotNone(first.next_cursor)
        # exactly full last page (250 rows => 3 pages of 100/100/50)
        p2 = fetch_page(conn, **kwargs(cursor=first.next_cursor, limit=100))
        self.assertEqual([rid for rid, _ in p2.items], list(range(101, 201)))
        p3 = fetch_page(conn, **kwargs(cursor=p2.next_cursor, limit=100))
        self.assertEqual([rid for rid, _ in p3.items], list(range(201, 251)))
        self.assertIsNone(p3.next_cursor)

    def test_exact_multiple_no_empty_trailing_page(self):
        conn = make_db(200)
        p1 = fetch_page(conn, **kwargs(limit=100))
        p2 = fetch_page(conn, **kwargs(cursor=p1.next_cursor, limit=100))
        self.assertEqual(len(p2.items), 100)
        self.assertIsNone(p2.next_cursor)  # no spurious empty third page

    def test_full_scan_equivalent_to_one_shot_read(self):
        conn = make_db(3001, ties=7)
        ids = [rid for p in iter_pages(conn, **kwargs(limit=100)) for rid, _ in p.items]
        self.assertEqual(ids, full_ids(conn))
        self.assertEqual(len(ids), 3001)
        self.assertEqual(len(ids), len(set(ids)))  # no duplicates

    def test_ties_are_stable_secondary_sort(self):
        # 500 rows all sharing one sort key: order must be exactly id asc.
        conn = make_db(500, ties=10_000)
        ids = [rid for p in iter_pages(conn, **kwargs(limit=37)) for rid, _ in p.items]
        self.assertEqual(ids, list(range(1, 501)))

    def test_same_page_repeated_is_identical(self):
        # Boundary pages must not "wiggle": re-fetching with the same cursor
        # after commits in between returns the same window.
        conn = make_db(1000, ties=5)
        p1 = fetch_page(conn, **kwargs(limit=100))
        p2a = fetch_page(conn, **kwargs(cursor=p1.next_cursor, limit=100))
        conn.commit()
        p2b = fetch_page(conn, **kwargs(cursor=p1.next_cursor, limit=100))
        self.assertEqual(p2a.items, p2b.items)


class ConcurrentMutationTests(unittest.TestCase):
    def scan(self, conn, n, page_size, mutate):
        """Scan pages; mutate(page_index, cursor_position_key_id) between pages."""
        seen = []
        cursor = None
        page_idx = 0
        while True:
            page = fetch_page(conn, **kwargs(cursor=cursor, limit=page_size))
            seen.extend(rid for rid, _ in page.items)
            if page.next_cursor is None:
                break
            key, rid = decode_cursor(page.next_cursor, SECRET)
            mutate(page_idx, (key, rid))
            cursor = page.next_cursor
            page_idx += 1
        return seen

    def test_delete_during_scan(self):
        conn = make_db(2000)
        start = set(full_ids(conn))
        deleted = set()

        def mutate(idx, pos):
            if idx == 2:  # delete 120 rows strictly ahead of the cursor
                rows = conn.execute(
                    "SELECT id FROM items WHERE (created_at,id) > (?,?) "
                    "LIMIT 120", pos).fetchall()
                ids = [r[0] for r in rows]
                conn.executemany("DELETE FROM items WHERE id=?", [(i,) for i in ids])
                conn.commit()
                deleted.update(ids)

        seen = self.scan(conn, 2000, 100, mutate)
        # no duplicates ever; exactly the deleted-ahead rows are missing
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), start - deleted)
        self.assertTrue(deleted)

    def test_insert_during_scan(self):
        conn = make_db(2000)
        start = set(full_ids(conn))
        inserted_behind, inserted_ahead = [], []

        def mutate(idx, pos):
            if idx == 1:
                # key strictly behind cursor => must be skipped (explainable)
                behind = [(900_000 + j, pos[0] - 5, "behind") for j in range(30)]
                # key strictly ahead of cursor => must appear later
                ahead = [(910_000 + j, pos[0] + 5, "ahead") for j in range(40)]
                conn.executemany(
                    "INSERT INTO items (id, created_at, payload) VALUES (?,?,?)",
                    behind + ahead)
                conn.commit()
                inserted_behind.extend(r[0] for r in behind)
                inserted_ahead.extend(r[0] for r in ahead)

        seen = self.scan(conn, 2000, 100, mutate)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertTrue(set(inserted_ahead) <= set(seen))
        self.assertTrue(set(inserted_behind).isdisjoint(set(seen)))
        self.assertEqual(set(seen), start | set(inserted_ahead))

    def test_random_mutation_fuzz_no_dup_stable_rows_not_missed(self):
        rng = random.Random(20261005)
        conn = make_db(5000, ties=4)
        stable = set(full_ids(conn))     # rows we will never delete
        future_inserts = []
        next_id = 10_000_000
        max_key = conn.execute("SELECT MAX(created_at) FROM items").fetchone()[0]

        def mutate(idx, pos):
            # random deletes ahead of cursor
            victims = conn.execute(
                "SELECT id FROM items WHERE (created_at,id) > (?,?) AND id < 10000000 "
                "ORDER BY RANDOM() LIMIT 25", pos).fetchall()
            ids = [r[0] for r in victims]
            conn.executemany("DELETE FROM items WHERE id=?", [(i,) for i in ids])
            conn.commit()
            stable.difference_update(ids)
            # inserts with keys far beyond any existing row: cursor has not
            # reached them yet, so all must be observed (no misses).
            nonlocal next_id
            if idx % 3 == 0:
                batch = [(next_id + j, max_key + 1000 + idx, "f") for j in range(10)]
                next_id += 10
                conn.executemany(
                    "INSERT INTO items (id, created_at, payload) VALUES (?,?,?)", batch)
                conn.commit()
                future_inserts.extend(r[0] for r in batch)

        seen = self.scan(conn, 5000, 37, mutate)
        self.assertEqual(len(seen), len(set(seen)))           # never duplicated
        self.assertTrue(stable <= set(seen))                 # stable rows not missed
        self.assertTrue(set(future_inserts) <= set(seen))    # late inserts seen

    def test_tied_keys_with_insert_delete_at_boundary(self):
        # 200 rows share one key; mutate inside that tie group mid-scan.
        conn = make_db(200, ties=10_000)
        key0 = conn.execute("SELECT created_at FROM items LIMIT 1").fetchone()[0]
        seen = []
        cursor = None
        pages = 0
        while True:
            page = fetch_page(conn, **kwargs(cursor=cursor, limit=50))
            seen.extend(rid for rid, _ in page.items)
            if page.next_cursor is None:
                break
            if pages == 1:
                # delete 20 not-yet-seen ids inside tie group, insert 10 after
                conn.executemany("DELETE FROM items WHERE id=?",
                                 [(i,) for i in range(101, 121)])
                conn.executemany("INSERT INTO items (id, created_at, payload) VALUES (?,?,?)",
                                 [(900 + j, key0, "new") for j in range(10)])
                conn.commit()
            cursor = page.next_cursor
            pages += 1
        self.assertEqual(len(seen), len(set(seen)))
        # order inside the tie group is strictly ascending wherever present
        self.assertEqual(seen, sorted(seen))
        self.assertTrue({900 + j for j in range(10)} <= set(seen))


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Paginate while rows are inserted/deleted mid-scan, then explain EVERY
difference between the paged result and one-shot full reads (t0 and t1).

Exits non-zero if any difference is unexplained.
"""
import sqlite3
from cursorlib import decode_cursor, fetch_page

SECRET = b"demo-diff-secret"
N = 10_000
PAGE = 100


def full_ids(conn):
    return [r[0] for r in conn.execute("SELECT id FROM items ORDER BY created_at, id")]


def main():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, created_at INTEGER, payload TEXT)")
    conn.executemany(
        "INSERT INTO items (id, created_at, payload) VALUES (?,?,?)",
        [(i, 1_700_000_000 + i // 4, f"p{i}") for i in range(1, N + 1)])
    conn.commit()

    snap_t0 = full_ids(conn)  # one-shot full read BEFORE the scan

    deleted_ahead, deleted_seen = [], []
    inserted_behind, inserted_ahead = [], []

    paged, cursor, page_idx = [], None, 0
    while True:
        page = fetch_page(conn, table="items", key_col="created_at", id_col="id",
                          secret=SECRET, cursor=cursor, limit=PAGE)
        paged.extend(rid for rid, _ in page.items)
        if page.next_cursor is None:
            break
        pos = decode_cursor(page.next_cursor, SECRET)
        if page_idx == 2:
            # delete 50 rows strictly AHEAD of the cursor
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM items WHERE (created_at,id) > (?,?) LIMIT 50", pos)]
            conn.executemany("DELETE FROM items WHERE id=?", [(i,) for i in ids])
            deleted_ahead += ids
        if page_idx == 4:
            # delete 20 rows we ALREADY emitted (behind the cursor)
            ids = paged[-20:]
            conn.executemany("DELETE FROM items WHERE id=?", [(i,) for i in ids])
            deleted_seen += ids
        if page_idx == 5:
            # insert 30 rows BEHIND the cursor (key range already passed)
            rows = [(200_000 + j, pos[0] - 3, "behind") for j in range(30)]
            conn.executemany("INSERT INTO items VALUES (?,?,?)", rows)
            inserted_behind += [r[0] for r in rows]
        if page_idx == 6:
            # insert 40 rows AHEAD of the cursor
            rows = [(300_000 + j, pos[0] + 3, "ahead") for j in range(40)]
            conn.executemany("INSERT INTO items VALUES (?,?,?)", rows)
            inserted_ahead += [r[0] for r in rows]
        conn.commit()
        cursor = page.next_cursor
        page_idx += 1

    snap_t1 = full_ids(conn)  # one-shot full read AFTER the scan

    paged_set, t0_set, t1_set = set(paged), set(snap_t0), set(snap_t1)
    assert len(paged) == len(paged_set), "DUPLICATES in paged result!"

    # --- difference ledger: every diff item must map to exactly one cause ---
    ledger = []

    def record(category, ids):
        for i in sorted(ids):
            ledger.append((i, category))

    # paged vs snapshot@t0
    record("deleted_ahead_of_cursor (in t0, not paged)", t0_set - paged_set)
    record("inserted_ahead_of_cursor (paged, not in t0)", paged_set - t0_set)
    # paged vs snapshot@t1
    record("deleted_after_being_emitted (paged, not in t1)", paged_set - t1_set)
    record("inserted_behind_cursor (in t1, not paged)", t1_set - paged_set)

    # verify each ledger entry matches the scripted mutation sets
    da, ds = set(deleted_ahead), set(deleted_seen)
    ib, ia = set(inserted_behind), set(inserted_ahead)
    unexplained = []
    for rid, cat in ledger:
        ok = (
            (cat.startswith("deleted_ahead") and rid in da)
            or (cat.startswith("inserted_ahead") and rid in ia)
            or (cat.startswith("deleted_after") and rid in ds)
            or (cat.startswith("inserted_behind") and rid in ib)
        )
        if not ok:
            unexplained.append((rid, cat))
    # and the reverse direction: every mutation is accounted for
    assert da == (t0_set - paged_set), "deleted-ahead set mismatch"
    assert ia == (paged_set - t0_set), "inserted-ahead set mismatch"
    assert ds == (paged_set - t1_set), "deleted-after-emit set mismatch"
    assert ib == (t1_set - paged_set), "inserted-behind set mismatch"
    assert not unexplained, f"UNEXPLAINED DIFFS: {unexplained[:5]}"

    # --- report ---
    print(f"rows at t0={len(snap_t0)}  paged={len(paged)}  rows at t1={len(snap_t1)}")
    print(f"pages fetched={page_idx + 1}  page_size={PAGE}")
    print("\ndifference ledger (paged scan vs one-shot reads):")
    cats = {}
    for rid, cat in ledger:
        cats.setdefault(cat, []).append(rid)
    for cat, ids in cats.items():
        sample = ", ".join(map(str, ids[:5])) + (" ..." if len(ids) > 5 else "")
        print(f"  {cat:<48} n={len(ids):<4} ids: {sample}")
    print(f"\nTOTAL diff items={len(ledger)}  UNEXPLAINED=0  -> all differences accounted for")


if __name__ == "__main__":
    main()

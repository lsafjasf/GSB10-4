"""Paged scan vs one-shot full read under concurrent insert/delete.

Runs a keyset-paged scan while a writer thread mutates the table, then
compares the paged result against a full snapshot taken at scan end.  Every
differing row is explained from the mutation log, and the two stability
invariants are asserted:

1. NO DUPLICATES  -- the paged output never contains the same id twice.
2. NO MISSES FOR STABLE ROWS -- every row present at scan start that is
   never deleted appears in the paged output exactly once.

Differences vs the end-of-scan snapshot are expected and fully accountable:
rows deleted after being read, rows inserted behind the cursor, rows
inserted ahead but deleted before the snapshot, etc.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import statistics
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field

from cursor_pager import QuerySpec, fetch_page

SECRET = b"diff-experiment-secret"
ROWS_AT_START = 20_000
PAGE_SIZE = 500
TIE_MODULO = 2_000  # k in [0, 2000): heavy ties on the sort key


@dataclass
class Mutation:
    seq: int
    op: str            # "insert" | "delete"
    row_id: int
    k: int | None      # sort key (None for deletes of unknown key)
    ahead_of_cursor: bool | None  # position vs cursor at mutation time


@dataclass
class Shared:
    cursor_pos: tuple[int, int] | None = None  # last emitted (k, id)
    lock: threading.Lock = field(default_factory=threading.Lock)
    log: list[Mutation] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)


def build_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, k INTEGER NOT NULL, "
        "label TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX idx_items_k_id ON items(k, id)")
    rng = random.Random(7)
    conn.executemany(
        "INSERT INTO items(id, k, label) VALUES(?,?,?)",
        [(i, rng.randrange(TIE_MODULO), f"seed-{i}")
         for i in range(ROWS_AT_START)],
    )
    conn.commit()
    conn.close()


def writer(path: str, shared: Shared, seed_keys: dict[int, int]) -> None:
    rng = random.Random(99)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    next_id = ROWS_AT_START
    live_inserts: list[tuple[int, int]] = []  # (id, k) inserted by writer
    seq = 0
    while not shared.stop.is_set():
        with shared.lock:
            pos = shared.cursor_pos
        if rng.random() < 0.55:
            k = rng.randrange(TIE_MODULO)
            conn.execute(
                "INSERT INTO items(id, k, label) VALUES(?,?,?)",
                (next_id, k, f"live-{next_id}"),
            )
            conn.commit()
            ahead = None if pos is None else (k, next_id) > pos
            shared.log.append(Mutation(seq, "insert", next_id, k, ahead))
            live_inserts.append((next_id, k))
            next_id += 1
        else:
            pool = [i for i in range(ROWS_AT_START) if i in seed_keys]
            if live_inserts and rng.random() < 0.3:
                victim, vk = live_inserts.pop(rng.randrange(len(live_inserts)))
                k = vk
            elif pool:
                victim = pool[rng.randrange(len(pool))]
                k = seed_keys.pop(victim)
            else:
                time.sleep(0.001)
                continue
            cur = conn.execute("DELETE FROM items WHERE id = ?", (victim,))
            conn.commit()
            if cur.rowcount:
                ahead = None if pos is None else (k, victim) > pos
                shared.log.append(Mutation(seq, "delete", victim, k, ahead))
        seq += 1
        time.sleep(0.0005)
    conn.close()


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="cursor_diff_")
    path = f"{tmp}/exp.db"
    build_db(path)

    seed_keys = {
        r[0]: r[1]
        for r in sqlite3.connect(path).execute("SELECT id, k FROM items")
    }
    initial_ids = set(seed_keys)
    shared = Shared()
    t = threading.Thread(target=writer, args=(path, shared, seed_keys),
                         daemon=True)
    t.start()

    # ---- paged scan (reader) ----
    rconn = sqlite3.connect(path, timeout=10)
    rconn.execute("PRAGMA busy_timeout=10000")
    spec = QuerySpec("items", "k", "id")
    paged_ids: list[int] = []
    lat_ms: list[float] = []
    token = None
    pages = 0
    t0 = time.perf_counter()
    while True:
        s = time.perf_counter()
        page = fetch_page(rconn, spec, PAGE_SIZE, SECRET, token)
        lat_ms.append((time.perf_counter() - s) * 1000)
        paged_ids.extend(r["id"] for r in page.rows)
        if page.rows:
            last = page.rows[-1]
            with shared.lock:
                shared.cursor_pos = (last["k"], last["id"])
        pages += 1
        token = page.next_cursor
        if token is None:
            break
        time.sleep(0.03)  # simulate network/app latency between page requests
    scan_s = time.perf_counter() - t0
    shared.stop.set()
    t.join(timeout=10)

    # ---- one-shot full read at scan end ----
    snapshot_ids = {r[0] for r in rconn.execute("SELECT id FROM items")}
    paged_set = set(paged_ids)

    # ---- invariants ----
    dupes = len(paged_ids) - len(paged_set)
    deleted_ids = {m.row_id for m in shared.log if m.op == "delete"}
    stable_ids = initial_ids - deleted_ids
    missing_stable = stable_ids - paged_set

    # ---- explain every difference vs the snapshot ----
    only_paged = paged_set - snapshot_ids      # read, then deleted
    only_snapshot = snapshot_ids - paged_set   # inserted behind cursor
    inserts = {m.row_id: m for m in shared.log if m.op == "insert"}

    unexplained: list[int] = []
    for rid in only_paged:
        if rid not in deleted_ids:
            unexplained.append(rid)
    behind = sum(
        1 for rid in only_snapshot
        if rid in inserts and inserts[rid].ahead_of_cursor is False
    )
    for rid in only_snapshot:
        m = inserts.get(rid)
        if m is None or m.ahead_of_cursor is not False:
            unexplained.append(rid)

    inserted_and_seen = sum(
        1 for rid, m in inserts.items()
        if rid in paged_set and rid in snapshot_ids
    )
    n_ins = sum(1 for m in shared.log if m.op == "insert")
    n_del = sum(1 for m in shared.log if m.op == "delete")

    print("=" * 66)
    print("DIFF EXPERIMENT: paged scan vs one-shot snapshot (concurrent I/D)")
    print("=" * 66)
    print(f"rows at start          : {ROWS_AT_START}")
    print(f"pages scanned          : {pages} (page_size={PAGE_SIZE})")
    print(f"scan wall time         : {scan_s:.2f}s")
    print(f"mutations during scan  : {n_ins} inserts, {n_del} deletes")
    print(f"paged rows delivered   : {len(paged_ids)}")
    print(f"snapshot rows at end   : {len(snapshot_ids)}")
    print("-" * 66)
    print("INVARIANTS")
    print(f"  duplicate deliveries : {dupes}  (guarantee: 0)")
    print(f"  stable rows missed   : {len(missing_stable)}  (guarantee: 0)")
    print("-" * 66)
    print("DIFF vs end-of-scan snapshot (every row explained)")
    print(f"  in paged, not in snapshot : {len(only_paged)}"
          f"  -> deleted after being read")
    print(f"  in snapshot, not in paged : {len(only_snapshot)}"
          f"  -> inserted BEHIND cursor ({behind} confirmed by log)")
    print(f"  inserted ahead & kept     : {inserted_and_seen}"
          f"  -> appears in BOTH (no diff)")
    print(f"  unexplained rows          : {len(unexplained)}  (must be 0)")

    # Per-row evidence dump: every differing id with the mutation that
    # explains it (sequence number, op, position relative to the cursor).
    details = {
        "config": {
            "rows_at_start": ROWS_AT_START,
            "page_size": PAGE_SIZE,
            "pages": pages,
            "tie_modulo": TIE_MODULO,
            "inserts_during_scan": n_ins,
            "deletes_during_scan": n_del,
            "scan_wall_seconds": round(scan_s, 3),
        },
        "paged_not_in_snapshot": [
            {"id": rid, "reason": "deleted_after_read",
             "delete_seq": next(
                 (m.seq for m in shared.log
                  if m.op == "delete" and m.row_id == rid), None),
             "origin": ("writer_inserted" if rid in inserts else "seed")}
            for rid in sorted(only_paged)
        ],
        "snapshot_not_in_paged": [
            {"id": rid, "reason": "inserted_behind_cursor",
             "insert_seq": inserts[rid].seq,
             "sort_key": inserts[rid].k}
            for rid in sorted(only_snapshot)
        ],
        "inserted_ahead_and_returned": sorted(
            rid for rid, m in inserts.items()
            if rid in paged_set and rid in snapshot_ids),
        "unexplained": sorted(unexplained),
    }
    os.makedirs("results", exist_ok=True)
    with open("results/diff_rows.json", "w") as f:
        json.dump(details, f, indent=1)
    print("  per-row evidence written : results/diff_rows.json")
    print("-" * 66)
    if lat_ms:
        qs = statistics.quantiles(lat_ms, n=100)
        print(f"page latency ms: p50={qs[49]:.2f} p95={qs[94]:.2f} "
              f"p99={qs[98]:.2f} max={max(lat_ms):.2f}")
    print("=" * 66)

    assert dupes == 0, "duplicate rows delivered!"
    assert not missing_stable, f"stable rows missed: {sorted(missing_stable)[:5]}"
    assert not unexplained, f"unexplained diffs: {unexplained[:5]}"
    print("ALL INVARIANTS HOLD: no duplicates, no missed stable rows, "
          "every diff explained.")


if __name__ == "__main__":
    main()

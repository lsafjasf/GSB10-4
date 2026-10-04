"""Benchmark: 1,000,000 rows, page size 100, full keyset scan.

Reports total wall time and the per-page latency distribution, plus a
sampled OFFSET/LIMIT comparison at several depths to show why keyset is
the right choice for deep pagination.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import statistics
import tempfile
import time

from cursor_pager import QuerySpec, fetch_page

SECRET = b"bench-secret"
TOTAL = 1_000_000
PAGE = 100
TIE_MODULO = 50_000  # avg 20 rows share each sort key -> ties everywhere


def build(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, k INTEGER NOT NULL, "
        "label TEXT NOT NULL)"
    )
    rng = random.Random(2024)
    t0 = time.perf_counter()
    batch = []
    for i in range(TOTAL):
        batch.append((i, rng.randrange(TIE_MODULO), f"row-{i}"))
        if len(batch) == 20_000:
            conn.executemany(
                "INSERT INTO items(id,k,label) VALUES(?,?,?)", batch)
            conn.commit()
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO items(id,k,label) VALUES(?,?,?)", batch)
        conn.commit()
    print(f"inserted {TOTAL:,} rows in {time.perf_counter()-t0:.1f}s")
    t0 = time.perf_counter()
    conn.execute("CREATE INDEX idx_items_k_id ON items(k, id)")
    conn.commit()
    print(f"index built in {time.perf_counter()-t0:.1f}s")
    conn.close()


def pct(sorted_vals: list[float], q: float) -> float:
    idx = min(len(sorted_vals) - 1, max(0, int(q * len(sorted_vals))))
    return sorted_vals[idx]


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="cursor_bench_")
    path = f"{tmp}/bench.db"
    build(path)

    conn = sqlite3.connect(path)
    spec = QuerySpec("items", "k", "id")

    # warm up page cache / planner
    warm = fetch_page(conn, spec, PAGE, SECRET)

    # ---- keyset full scan ----
    lat: list[float] = []
    token = None
    delivered = 0
    pages = 0
    t0 = time.perf_counter()
    while True:
        s = time.perf_counter()
        page = fetch_page(conn, spec, PAGE, SECRET, token)
        lat.append((time.perf_counter() - s) * 1000)
        delivered += len(page.rows)
        pages += 1
        token = page.next_cursor
        if token is None:
            break
    total_s = time.perf_counter() - t0
    lat.sort()

    assert delivered == TOTAL, f"expected {TOTAL}, got {delivered}"
    print("=" * 66)
    print(f"KEYSET SCAN: {TOTAL:,} rows, page_size={PAGE}, pages={pages:,}")
    print("=" * 66)
    print(f"total wall time : {total_s:.2f}s "
          f"({TOTAL/total_s:,.0f} rows/s, {pages/total_s:,.0f} pages/s)")
    print(f"per-page ms     : mean={statistics.fmean(lat):.3f} "
          f"min={lat[0]:.3f} p50={pct(lat,.5):.3f} p90={pct(lat,.9):.3f} "
          f"p95={pct(lat,.95):.3f} p99={pct(lat,.99):.3f} max={lat[-1]:.3f}")
    print(f"rows delivered  : {delivered:,} (exactly once each)")

    # ---- OFFSET comparison, sampled at increasing depths ----
    print("-" * 66)
    print("OFFSET/LIMIT comparison (sampled single pages at depth):")
    offset_samples = {}
    for depth in (0, 100_000, 300_000, 500_000, 700_000, 900_000, 990_000):
        s = time.perf_counter()
        conn.execute(
            "SELECT * FROM items ORDER BY k ASC, id ASC LIMIT ? OFFSET ?",
            (PAGE, depth),
        ).fetchall()
        ms = (time.perf_counter() - s) * 1000
        offset_samples[depth] = round(ms, 2)
        print(f"  depth {depth:>7,}: {ms:8.2f} ms/page")
    keyset_deep = pct(lat, 0.99)
    print(f"  keyset p99 at ANY depth: {keyset_deep:.2f} ms/page")

    os.makedirs("results", exist_ok=True)
    with open("results/bench.json", "w") as f:
        json.dump({
            "rows": TOTAL, "page_size": PAGE, "pages": pages,
            "total_seconds": round(total_s, 3),
            "page_latency_ms": {
                "mean": round(statistics.fmean(lat), 3),
                "min": round(lat[0], 3),
                "p50": round(pct(lat, .5), 3),
                "p90": round(pct(lat, .9), 3),
                "p95": round(pct(lat, .95), 3),
                "p99": round(pct(lat, .99), 3),
                "max": round(lat[-1], 3),
            },
            "offset_sample_ms": offset_samples,
            "all_page_latencies_ms": [round(x, 3) for x in lat],
        }, f)
    print("raw latencies written: results/bench.json")
    print("=" * 66)


if __name__ == "__main__":
    main()

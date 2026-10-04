"""Benchmark: 1,000,000 rows, page size 100, full forward scan.
Reports total time, per-page latency distribution, and verifies the paged
result equals a one-shot full read (no misses, no duplicates at rest).
"""
import sqlite3
import statistics
import time
from cursorlib import fetch_page

SECRET = b"bench-secret"
N = 1_000_000
PAGE = 100
TIES = 8  # rows per distinct sort key (realistic tie pressure)


def build():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, created_at INTEGER, payload TEXT)")
    t0 = time.perf_counter()
    conn.executemany(
        "INSERT INTO items (id, created_at, payload) VALUES (?,?,?)",
        ((i, 1_700_000_000 + i // TIES, "x" * 32) for i in range(1, N + 1)))
    conn.execute("CREATE INDEX idx_items_key ON items (created_at, id)")
    conn.commit()
    print(f"setup: inserted {N:,} rows + index in {time.perf_counter() - t0:.2f}s")
    return conn


def pct(sorted_vals, q):
    idx = min(len(sorted_vals) - 1, max(0, round(q / 100 * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def main():
    conn = build()

    t0 = time.perf_counter()
    baseline = [r[0] for r in conn.execute(
        "SELECT id FROM items ORDER BY created_at, id")]
    t_baseline = time.perf_counter() - t0

    lat = []
    seen = []
    cursor = None
    pages = 0
    t0 = time.perf_counter()
    while True:
        t1 = time.perf_counter()
        page = fetch_page(conn, table="items", key_col="created_at", id_col="id",
                          secret=SECRET, cursor=cursor, limit=PAGE)
        lat.append(time.perf_counter() - t1)
        seen.extend(rid for rid, _ in page.items)
        pages += 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    total = time.perf_counter() - t0

    assert seen == baseline, "paged scan != one-shot full read"
    assert len(seen) == len(set(seen)) == N

    lat_ms = sorted(v * 1000 for v in lat)
    print(f"\nrows={N:,}  page_size={PAGE}  pages={pages:,}")
    print(f"total scan time      : {total:.3f}s  ({N / total:,.0f} rows/s)")
    print(f"one-shot full read   : {t_baseline:.3f}s (for comparison)")
    print("per-page latency (ms):")
    print(f"  min={lat_ms[0]:.3f}  p50={pct(lat_ms,50):.3f}  p90={pct(lat_ms,90):.3f}"
          f"  p95={pct(lat_ms,95):.3f}  p99={pct(lat_ms,99):.3f}"
          f"  max={lat_ms[-1]:.3f}  mean={statistics.mean(lat_ms):.3f}")
    print(f"correctness: paged == one-shot full read, {N:,} unique rows, 0 dup, 0 miss")


if __name__ == "__main__":
    main()

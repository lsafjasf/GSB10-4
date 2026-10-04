"""Benchmark: decision latency and memory upper bound at 1,000,000 requests.

Phase 1: 1,000,000 unique keys (first requests) -> insert path latency,
         memory per record (tracemalloc + /proc RSS).
Phase 2: 1,000,000 replays of existing keys -> lookup/replay latency.

Run:  python3 bench/benchmark.py            (from the idempotency/ dir)
"""

import os
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from idempotency import IdempotencyStore, Response, request_fingerprint

N = 1_000_000
BODY = b'{"order_id":"O-1","status":"CREATED"}'
CANNED = Response(status=201, body=BODY, headers=(("Content-Type", "application/json"),))


def rss_bytes():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    return 0


def pct(sorted_ns, p):
    idx = min(len(sorted_ns) - 1, int(len(sorted_ns) * p))
    return sorted_ns[idx] / 1000.0  # ns -> us


def report(name, lat_ns):
    lat_ns.sort()
    total_s = sum(lat_ns) / 1e9
    print(f"[{name}] n={len(lat_ns)}")
    print(f"  total        : {total_s:.3f} s")
    print(f"  throughput   : {len(lat_ns)/total_s:,.0f} req/s")
    print(f"  mean         : {sum(lat_ns)/len(lat_ns)/1000.0:.2f} us")
    print(f"  p50          : {pct(lat_ns, 0.50):.2f} us")
    print(f"  p99          : {pct(lat_ns, 0.99):.2f} us")
    print(f"  max          : {lat_ns[-1]/1000.0:.2f} us")


def main():
    store = IdempotencyStore(ttl_seconds=3600)
    handler_calls = {"n": 0}

    def handler():
        handler_calls["n"] += 1
        return CANNED

    fps = []
    keys = []
    for i in range(N):
        key = f"bench-{i:016x}"
        keys.append(key)
        fps.append(request_fingerprint("POST", "/orders", key.encode()))

    # ---- Phase 1: 1M first requests -------------------------------------
    rss0 = rss_bytes()
    tracemalloc.start()
    lat = []
    t0 = time.perf_counter()
    for i in range(N):
        s = time.perf_counter_ns()
        store.execute("user-bench", "POST /orders", keys[i], fps[i], handler)
        lat.append(time.perf_counter_ns() - s)
    wall = time.perf_counter() - t0
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss1 = rss_bytes()

    assert handler_calls["n"] == N
    assert len(store) == N

    print("=" * 64)
    print("Phase 1: 1,000,000 first requests (unique keys)")
    print("=" * 64)
    report("first-request decision", lat)
    print(f"  wall clock   : {wall:.3f} s")
    print()
    print("  memory (store holds 1,000,000 completed records):")
    print(f"  tracemalloc current : {current/2**20:,.1f} MiB")
    print(f"  tracemalloc peak    : {peak/2**20:,.1f} MiB")
    print(f"  RSS delta           : {(rss1-rss0)/2**20:,.1f} MiB")
    print(f"  per record (malloc) : {current/N:,.0f} B")
    print(f"  per record (RSS)    : {(rss1-rss0)/N:,.0f} B")

    # ---- Phase 2: 1M replays --------------------------------------------
    lat2 = []
    t0 = time.perf_counter()
    for i in range(N):
        s = time.perf_counter_ns()
        resp, replayed = store.execute("user-bench", "POST /orders", keys[i], fps[i], handler)
        lat2.append(time.perf_counter_ns() - s)
        assert replayed and resp.status == 201 and resp.body == BODY
    wall2 = time.perf_counter() - t0
    assert handler_calls["n"] == N, "replays must never re-run the handler"

    print()
    print("=" * 64)
    print("Phase 2: 1,000,000 replays (existing keys)")
    print("=" * 64)
    report("replay decision", lat2)
    print(f"  wall clock   : {wall2:.3f} s")

    rss2 = rss_bytes()
    print()
    print(f"final RSS: {rss2/2**20:,.1f} MiB, records in store: {len(store):,}")


if __name__ == "__main__":
    main()

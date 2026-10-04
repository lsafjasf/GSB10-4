"""Benchmark for idempotency.py (stdlib only).

Usage:
    python3 benchmark.py                 # timing for 1,000,000 decisions
    python3 benchmark.py --rss completed # RSS after N COMPLETED records
    python3 benchmark.py --rss tombstone # RSS after N EXPIRED tombstones
    python3 benchmark.py --help
"""
import array
import gc
import resource
import sys
import time

from idempotency import IdempotencyStore, make_key, fingerprint_payload

N = 1_000_000
BODY_LEN = 64
_TTL = 24 * 3600.0
_TOMB = 7 * 24 * 3600.0


def body_for(i):
    base = b'{"order_id":"ORD-%07d","status":"CREATED"}' % i
    return base + b" " * (BODY_LEN - len(base))


def rss_mb():
    # current resident set (ru_maxrss is a peak and never decreases)
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * 4096 / 1024 / 1024


def pct(sorted_arr, p):
    return sorted_arr[int(len(sorted_arr) * p) - 1]


def run_timing():
    store = IdempotencyStore(ttl=_TTL, tombstone_ttl=_TOMB)
    keys = [make_key("u%d" % i, "POST /orders", "ck-%d" % i) for i in range(N)]
    fps = [fingerprint_payload(body_for(i)) for i in range(N)]
    bodies = [body_for(i) for i in range(N)]
    gc.collect(); gc.disable()

    lat = array.array("Q")
    t0 = time.perf_counter_ns()
    for i in range(N):
        a = time.perf_counter_ns()
        store.begin(keys[i], fps[i])
        store.complete(keys[i], fps[i], 201, bodies[i])
        lat.append(time.perf_counter_ns() - a)
    t_first = time.perf_counter_ns() - t0

    lat_r = array.array("Q")
    t0 = time.perf_counter_ns()
    for i in range(N):
        a = time.perf_counter_ns()
        r = store.begin(keys[i], fps[i])
        lat_r.append(time.perf_counter_ns() - a)
        assert r.decision.value == "replay"
    t_replay = time.perf_counter_ns() - t0
    gc.enable()

    print("N = %d, distinct 64 B bodies/fingerprints, GC disabled in loop" % N)
    print("-" * 60)
    s1 = sorted(lat); s2 = sorted(lat_r)
    def row(name, total, a, s):
        print("%-20s %7.3f s  avg %5.0f ns  p50 %5.0f ns  "
              "p99 %6.0f ns  max %6.0f us  %.0f ops/s"
              % (name, total / 1e9,
                 total / N, pct(s, .5), pct(s, .99), max(s) / 1e3,
                 N / (total / 1e9)))
    row("first begin+complete", t_first, None, s1)
    row("replay begin", t_replay, None, s2)


def run_rss(state):
    gc.collect()
    base = rss_mb()
    store = IdempotencyStore(ttl=10.0**9, tombstone_ttl=10.0**9)
    # nothing is retained outside the store; each body/fp is distinct
    for i in range(N):
        k = make_key("u%d" % i, "POST /orders", "ck-%d" % i)
        body = body_for(i)
        fp = fingerprint_payload(body)
        store.begin(k, fp)
        if state == "tombstone":
            # expire directly: no body was ever stored (fingerprint kept)
            rec = store._records[k]
            rec.state = 2  # _EXPIRED
            rec.tombstone_until = rec.expires_at + _TOMB
        else:
            store.complete(k, fp, 201, body)
    used = rss_mb()
    per = (used - base) * 1024 * 1024 / N
    print("state=%s N=%d RSS delta=%.0f MB  -> %.0f bytes/key"
          % (state, N, used - base, per))
    print("upper bound for 1e6 keys: %.0f MB" % (per * 1e6 / 1024 / 1024))


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--rss":
        run_rss(args[1] if len(args) > 1 else "completed")
    elif args and args[0] == "--help":
        print(__doc__)
    else:
        run_timing()

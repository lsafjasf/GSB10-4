"""Benchmark: 1M schedules - insert rate, scan cost vs backlog, memory peak.

Run:  python3 bench/benchmark.py
"""

import os
import random
import resource
import shutil
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from delayq import DelayedQueue

N = 1_000_000
FAR = 10**9  # far-future due times: keeps the backlog pending during scans


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def bench_insert_durable(n=20_000):
    d = tempfile.mkdtemp(prefix="delayq-bench-durable-")
    try:
        q = DelayedQueue(d, durable=True)
        t0 = time.perf_counter()
        for i in range(n):
            q.schedule(f"d{i}", FAR + i)
        dt = time.perf_counter() - t0
        q.close()
        return n / dt
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    print(f"== DelayQ benchmark, N={N:,} ==")
    print(f"machine: {os.uname().sysname} {os.uname().machine}, "
          f"python {sys.version.split()[0]}")

    rate = bench_insert_durable()
    print(f"[durable insert] 20,000 schedules with fsync: {rate:,.0f} ops/s")

    d = tempfile.mkdtemp(prefix="delayq-bench-")
    try:
        tracemalloc.start()
        q = DelayedQueue(d, durable=False)

        rng = random.Random(7)
        t0 = time.perf_counter()
        for i in range(N):
            q.schedule(i, FAR + rng.randrange(FAR), None)
        t_insert = time.perf_counter() - t0
        cur, peak = tracemalloc.get_traced_memory()
        print(f"[insert x{N:,}] {t_insert:.2f}s total, "
              f"{N / t_insert:,.0f} ops/s "
              f"({t_insert / N * 1e6:.2f} us/op)")
        print(f"[memory] tracemalloc peak {peak / 2**20:.1f} MiB, "
              f"process RSS peak {rss_mb():.1f} MiB "
              f"({rss_mb() * 2**20 / N:.0f} B/msg RSS)")

        # --- scan cost vs backlog -------------------------------------
        # 1) pump with nothing due and 1M pending: must be ~O(1).
        t0 = time.perf_counter()
        got = q.pump(FAR - 1, lambda r: None)
        t_empty = time.perf_counter() - t0
        assert not got
        print(f"[scan, 0 due / {N:,} backlog] {t_empty * 1e6:.1f} us")

        # 2) deliver k messages on top of the 1M-message backlog.
        k = 10_000
        for i in range(k):
            q.schedule(f"now{i}", 0)
        t0 = time.perf_counter()
        got = q.pump(0, lambda r: None)
        t_k_big = time.perf_counter() - t0
        assert len(got) == k
        print(f"[scan, {k:,} due / {N:,} backlog] {t_k_big:.3f}s "
              f"({t_k_big / k * 1e6:.2f} us/delivered)")

        # 3) same k on a fresh small queue: per-delivery cost must match.
        d2 = tempfile.mkdtemp(prefix="delayq-bench-small-")
        try:
            q2 = DelayedQueue(d2, durable=False)
            for i in range(k):
                q2.schedule(f"s{i}", 0)
            t0 = time.perf_counter()
            q2.pump(0, lambda r: None)
            t_k_small = time.perf_counter() - t0
            q2.close()
        finally:
            shutil.rmtree(d2, ignore_errors=True)
        print(f"[scan, {k:,} due / {k:,} backlog] {t_k_small:.3f}s "
              f"({t_k_small / k * 1e6:.2f} us/delivered)")

        # --- persistence: snapshot + reload ---------------------------
        t0 = time.perf_counter()
        q.snapshot()
        t_snap = time.perf_counter() - t0
        snap_sz = os.path.getsize(os.path.join(d, "snapshot.jsonl"))
        print(f"[snapshot {N:,} pending] {t_snap:.2f}s, "
              f"file {snap_sz / 2**20:.1f} MiB")
        q.close()

        t0 = time.perf_counter()
        q3 = DelayedQueue(d, durable=False)
        t_load = time.perf_counter() - t0
        assert q3.pending_count() == N
        print(f"[recover {N:,} pending] {t_load:.2f}s")
        q3.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()

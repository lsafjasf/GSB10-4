"""Overhead benchmark: 1,000,000 acquire/release cycles.

Measures wall time, per-op overhead, peak tracked memory (tracemalloc) and
process RSS delta, in two modes:
  - production: capture_stacks=False (site = explicit label only)
  - debug:      capture_stacks=True  (full acquire stack per handle)

Also measures leak-check cost with 10k simultaneously live handles.

Run: python3 bench.py
"""

import gc
import resource
import time
import tracemalloc

from leakdet import LeakTracker

N = 1_000_000


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def bench_mode(capture_stacks):
    gc.collect()
    t = LeakTracker(capture_stacks=capture_stacks, release_log_size=1000)
    tracemalloc.start()
    rss0 = rss_mb()
    t0 = time.perf_counter()
    for _ in range(N):
        t.acquire(kind="connection", label="db:bench").release()
    t1 = time.perf_counter()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss1 = rss_mb()
    acc = t.accounting()
    assert acc["acquired"] == N and acc["released"] == N and acc["live"] == 0, acc
    dt = t1 - t0
    return {
        "total_s": dt,
        "per_pair_us": dt / N * 1e6,
        "tracemalloc_peak_mb": peak / 1e6,
        "rss_delta_mb": rss1 - rss0,
    }


def bench_leak_check(live_count=10_000):
    t = LeakTracker(capture_stacks=False)
    handles = [t.acquire(kind="connection", label="db:live") for _ in range(live_count)]
    t0 = time.perf_counter()
    report = t.check_leaks(max_age=0.0)
    t1 = time.perf_counter()
    assert len(report.leaks) == live_count
    for h in handles:
        h.release()
    return (t1 - t0) * 1e3


def main():
    print(f"benchmark: {N:,} acquire/release pairs, Python "
          f"{__import__('sys').version.split()[0]}")
    for name, cs in [("production (no stacks)", False), ("debug (stacks, depth 8)", True)]:
        r = bench_mode(cs)
        print(f"  {name}:")
        print(f"    total time        : {r['total_s']:.2f} s")
        print(f"    per acquire+release: {r['per_pair_us']:.2f} us")
        print(f"    tracemalloc peak  : {r['tracemalloc_peak_mb']:.2f} MB "
              f"(tracker-internal, bounded)")
        print(f"    process RSS delta : {r['rss_delta_mb']:.2f} MB")
    ms = bench_leak_check()
    print(f"  leak check over 10,000 live handles: {ms:.2f} ms")
    print("  memory bound: O(live handles) + O(release_log_size=1000) + O(1) counters.")


if __name__ == "__main__":
    main()

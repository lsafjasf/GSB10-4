"""Benchmark: 100k sessions, 1M selections. Run: python3 benchmark.py"""

import os
import random
import resource
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sticky import StickyRouter

N_SESSIONS = 100_000
N_SELECTS = 1_000_000
BACKENDS = [f"b{i}" for i in range(8)]


def fmt_bytes(n):
    return f"{n / (1 << 20):.1f} MiB"


def main():
    lines = []
    out = lines.append
    out(f"python {sys.version.split()[0]}  backends={len(BACKENDS)}  "
        f"sessions={N_SESSIONS:,}  selects={N_SELECTS:,}")

    t0 = time.perf_counter()
    router = StickyRouter(BACKENDS)
    out(f"ring build (8 backends x 160 vnodes): {time.perf_counter() - t0:.3f} s")

    tracemalloc.start()
    sessions = [f"sess-{i:06d}" for i in range(N_SESSIONS)]

    t0 = time.perf_counter()
    for sid in sessions:
        router.select(sid)
    place_s = time.perf_counter() - t0
    out(f"placement phase: {N_SESSIONS:,} new sessions in {place_s:.3f} s "
        f"({N_SESSIONS / place_s:,.0f} selects/s)")

    # Small state per session so memory reflects a realistic sticky table.
    t0 = time.perf_counter()
    for sid in sessions:
        router.put_state(sid, {"uid": sid, "cart": ["a", "b"], "hits": 3})
    out(f"state writes: {N_SESSIONS:,} in {time.perf_counter() - t0:.3f} s")

    rng = random.Random(42)
    t0 = time.perf_counter()
    for _ in range(N_SELECTS):
        router.select(rng.choice(sessions))
    steady_s = time.perf_counter() - t0
    out(f"steady-state phase: {N_SELECTS:,} selects in {steady_s:.3f} s "
        f"({N_SELECTS / steady_s:,.0f} selects/s, {steady_s / N_SELECTS * 1e6:.2f} us/select)")

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    out(f"tracemalloc current={fmt_bytes(current)} peak={fmt_bytes(peak)}")
    out(f"process max RSS={fmt_bytes(rss)}")
    out(f"per-session router overhead ~= {current / N_SESSIONS:.0f} B "
        f"(pins + loads + per-session state snapshot)")

    # Drift cost: kill one backend, re-select every orphaned session.
    victim = max(BACKENDS, key=lambda b: router.load[b])
    orphaned = [sid for sid, b in router.pins.items() if b == victim]
    router.set_health(victim, False)
    t0 = time.perf_counter()
    for sid in orphaned:
        router.select(sid)
    drift_s = time.perf_counter() - t0
    lost = sum(1 for rec in router.history if rec.state_lost)
    out(f"drift: {victim} down -> migrated {len(orphaned):,} sessions in {drift_s:.3f} s, "
        f"state_lost={lost}")
    loads = {b: router.load[b] for b in BACKENDS}
    out(f"final loads: {loads}")

    text = "\n".join(lines)
    print(text)
    os.makedirs("reports", exist_ok=True)
    with open("reports/benchmark.txt", "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print("\n[written] reports/benchmark.txt")


if __name__ == "__main__":
    main()

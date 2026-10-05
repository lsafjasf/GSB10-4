"""Benchmark: 1,000,000 ops with several long transactions.

Outputs:
    out/benchmark.csv        -- per-sample metrics (curve data)
    stdout                   -- ASCII curves of version count / GC time / memory

Run:  python3 bench/benchmark.py
"""
import os
import random
import resource
import sys
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from longtxn import GCCoordinator, ManualClock, MVCCStore, TxnManager

OPS = 1_000_000
KEYS = 10_000
SAMPLE_EVERY = 25_000
DT_PER_OP = 0.001  # 1ms of injected time per op -> total 1000s

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")


def rss_kb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def spark(values, width=60):
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    blocks = "▁▂▃▄▅▆▇█"
    line = "".join(blocks[min(7, int((v - lo) / span * 7.999))] for v in values)
    return f"{line}  min={lo:.1f} max={hi:.1f}"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rng = random.Random(42)
    clock = ManualClock(0.0)
    store = MVCCStore()
    mgr = TxnManager(clock.now)  # long=30s, kill=120s
    coord = GCCoordinator(store, mgr)

    # 若干长事务：3 个从 t=0 开始，1 个从 t=300s 开始
    long_txns = [mgr.begin() for _ in range(3)]
    late_long_txn = None
    killed_total = 0

    tracemalloc.start()
    rows = []
    short_ops = 0

    for op in range(1, OPS + 1):
        key = rng.randrange(KEYS)
        store.put(key, f"v{op}", clock.advance(DT_PER_OP))

        # 短事务：每 1000 次操作一批，开始即提交
        short_ops += 1
        if short_ops == 1000:
            t = mgr.begin()
            mgr.commit(t)
            short_ops = 0

        if late_long_txn is None and clock.now() >= 300.0:
            late_long_txn = mgr.begin()

        if op % SAMPLE_EVERY == 0:
            scan = mgr.scan()
            killed_total += len(scan.killed)
            stats = coord.run_gc()
            cur, _peak = tracemalloc.get_traced_memory()
            rows.append({
                "ops": op,
                "time_s": round(clock.now(), 1),
                "water_level_s": round(stats.water_level, 1),
                "versions": store.version_count(),
                "removed": stats.removed_versions,
                "gc_ms": round(stats.elapsed_ms, 3),
                "py_heap_kb": cur // 1024,
                "maxrss_kb": rss_kb(),
                "active": len(mgr.active_txns()),
                "killed_total": killed_total,
            })

    # 收尾：强制处置仍存活的长事务，做最终清理
    for t in mgr.active_txns():
        mgr.force_abort(t)
        killed_total += 1
    final = coord.run_gc()
    cur, _ = tracemalloc.get_traced_memory()
    rows.append({
        "ops": OPS, "time_s": round(clock.now(), 1),
        "water_level_s": round(final.water_level, 1),
        "versions": store.version_count(), "removed": final.removed_versions,
        "gc_ms": round(final.elapsed_ms, 3), "py_heap_kb": cur // 1024,
        "maxrss_kb": rss_kb(), "active": 0, "killed_total": killed_total,
    })
    tracemalloc.stop()

    csv_path = os.path.join(OUT_DIR, "benchmark.csv")
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(",".join(rows[0].keys()) + "\n")
        for r in rows:
            f.write(",".join(str(v) for v in r.values()) + "\n")

    print(f"ops={OPS} keys={KEYS} long_txns=4 (3@t=0s, 1@t=300s) "
          f"thresholds: long>=30s kill>=120s")
    print(f"force-aborted total: {killed_total}")
    print(f"csv: {csv_path}\n")
    print("versions   :", spark([r["versions"] for r in rows]))
    print("gc_ms      :", spark([r["gc_ms"] for r in rows]))
    print("py_heap_kb :", spark([r["py_heap_kb"] for r in rows]))
    print("maxrss_kb  :", spark([r["maxrss_kb"] for r in rows]))
    print()
    hdr = list(rows[0].keys())
    print(("{:>8}" * len(hdr)).format(*hdr))
    for r in rows[::4] + [rows[-1]]:
        print(("{:>8}" * len(r)).format(*r.values()))


if __name__ == "__main__":
    main()

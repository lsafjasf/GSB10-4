"""基准：100 万次操作 + 若干长事务，输出内存占用与清理耗时曲线。

模型：每次操作 = 一个短事务(begin/write/commit)，时钟每操作推进 1ms。
每 10 万操作注入一个长事务（只读/有写交替），超过 max_duration 被处置。
每 2.5 万操作跑一次 GC。耗时：逻辑时钟(注入) + perf_counter(墙钟) 双口径。
"""

import csv
import os
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from longtxn import GarbageCollector, ManualClock, TransactionManager, VersionStore

OPS = 1_000_000
KEY_SPACE = 2_000
LONG_TXN_EVERY = 100_000      # 每 10 万操作注入一个长事务
ENFORCE_EVERY = 1_000
GC_EVERY = 25_000
SAMPLE_EVERY = 10_000
LONG_THRESHOLD = 30.0         # 30s 识别阈值
MAX_DURATION = 120.0          # 120s 处置阈值
DT_PER_OP = 0.001             # 每操作 1ms


def sparkline(values, width=60):
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    blocks = "▁▂▃▄▅▆▇█"
    line = ""
    for v in values:
        idx = int((v - lo) / span * (len(blocks) - 1))
        line += blocks[idx]
    return line


def main():
    clock = ManualClock()
    store = VersionStore()
    mgr = TransactionManager(store, clock, long_threshold=LONG_THRESHOLD,
                             max_duration=MAX_DURATION)
    gc = GarbageCollector(store, mgr)
    tracemalloc.start()

    samples = []
    cum_gc_logical = 0.0
    cum_gc_real = 0.0
    gc_passes = 0
    long_txns_started = 0
    t_start = time.perf_counter()

    for op in range(1, OPS + 1):
        t = mgr.begin()
        mgr.write(t, f"key{op % KEY_SPACE}", f"val{op}")
        mgr.commit(t)
        clock.advance(DT_PER_OP)

        if op % LONG_TXN_EVERY == LONG_TXN_EVERY // 2:
            lt = mgr.begin()
            if long_txns_started % 2 == 1:
                mgr.write(lt, f"key{op % KEY_SPACE}", "long-dirty")
            long_txns_started += 1

        if op % ENFORCE_EVERY == 0:
            mgr.enforce()

        if op % GC_EVERY == 0:
            t0 = time.perf_counter()
            stats = gc.collect()
            cum_gc_real += time.perf_counter() - t0
            cum_gc_logical += stats.logical_seconds
            gc_passes += 1

        if op % SAMPLE_EVERY == 0:
            cur, peak = tracemalloc.get_traced_memory()
            samples.append({
                "op": op,
                "sim_time_s": round(clock.now(), 1),
                "live_versions": store.total_versions(),
                "mem_mb": round(cur / 1e6, 2),
                "watermark_lag": mgr.commit_seq - mgr.watermark(),
                "active_long": len(mgr.identify_long_txns()),
                "cum_gc_logical_s": round(cum_gc_logical, 3),
                "cum_gc_real_s": round(cum_gc_real, 4),
            })

    total_real = time.perf_counter() - t_start
    tracemalloc.stop()

    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "bench_results.csv")
    with open(csv_path, "w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(samples[0].keys()))
        writer.writeheader()
        writer.writerows(samples)

    mems = [s["mem_mb"] for s in samples]
    versions = [s["live_versions"] for s in samples]
    gc_real = [s["cum_gc_real_s"] for s in samples]
    gc_log = [s["cum_gc_logical_s"] for s in samples]
    lag = [s["watermark_lag"] for s in samples]

    print(f"操作总数: {OPS:,}  长事务: {long_txns_started}  GC 轮次: {gc_passes}")
    print(f"总墙钟耗时: {total_real:.2f}s  峰值内存: {max(mems):.1f}MB  "
          f"峰值存活版本: {max(versions):,}")
    print()
    print(f"内存占用曲线 (MB, {mems[0]:.1f}..{max(mems):.1f}, 每点=1万操作):")
    print("  " + sparkline(mems))
    print(f"存活版本曲线 (个, 峰值 {max(versions):,}):")
    print("  " + sparkline(versions))
    print(f"水位滞后曲线 (commit_seq - watermark, 峰值 {max(lag):,}):")
    print("  " + sparkline(lag))
    print(f"GC 累计墙钟耗时曲线 (s, 合计 {gc_real[-1]:.3f}s):")
    print("  " + sparkline(gc_real))
    print(f"GC 累计逻辑耗时曲线 (注入时钟 s, 合计 {gc_log[-1]:.2f}s):")
    print("  " + sparkline(gc_log))
    print()
    print("采样明细(每 10 万操作取一行):")
    hdr = ("op", "sim_s", "live_ver", "mem_mb", "wm_lag", "long", "gc_real_s")
    print("  " + "  ".join(f"{h:>10}" for h in hdr))
    for s in samples:
        if s["op"] % 100_000 == 0:
            print("  " + "  ".join(f"{v:>10}" for v in (
                f"{s['op']:,}", s["sim_time_s"], f"{s['live_versions']:,}",
                s["mem_mb"], f"{s['watermark_lag']:,}", s["active_long"],
                s["cum_gc_real_s"])))
    print(f"\nCSV 已写入: {csv_path}")


if __name__ == "__main__":
    main()

"""100 万条消息基准：提交耗时 + 位点表内存上界（仅标准库测量）。"""

import gc
import json
import os
import random
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from offset_commit import OffsetTable, WalCommitter  # noqa: E402

N = 1_000_000
HERE = os.path.dirname(os.path.abspath(__file__))


def measure(name, fn):
    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()
    extra = fn()
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    row = {"scenario": name, "messages": N, "seconds": round(elapsed, 4),
           "per_msg_us": round(elapsed / N * 1e6, 3),
           "peak_bytes": peak, **(extra or {})}
    print(json.dumps(row, ensure_ascii=False))
    return row


def scenario_increasing():
    t = OffsetTable()
    for off in range(N):
        t.mark_done(off)
        t.advance()
    return {"final_base": t.base, "intervals_peak": 1,
            "intervals_end": len(t.intervals)}


def scenario_windowed_disorder(window=1024):
    rng = random.Random(42)
    t = OffsetTable()
    peak_intervals = 0
    for start in range(0, N, window):
        block = list(range(start, min(start + window, N)))
        rng.shuffle(block)
        for off in block:
            t.mark_done(off)
            if len(t.intervals) > peak_intervals:
                peak_intervals = len(t.intervals)
        t.advance()
    return {"final_base": t.base, "window": window,
            "intervals_peak": peak_intervals, "intervals_end": len(t.intervals)}


def scenario_batch_then_advance():
    t = OffsetTable()
    for off in range(N):
        t.mark_done(off)
    t.advance()
    return {"final_base": t.base, "intervals_peak": 1,
            "intervals_end": len(t.intervals)}


def scenario_sparse_holes():
    """最坏内存形状：完成/空洞交替 => 区间数 = 完成数。

    用 100k 完成位点实测单区间内存，线性外推到 100 万条消息
    （50 万区间）。list.insert 为 O(n)，故只测内存不测全量耗时。
    """
    m = 100_000
    t = OffsetTable()
    for off in range(0, 2 * m, 2):
        t.mark_done(off)
    t.advance()
    approx = sum(sys.getsizeof(iv) + 2 * sys.getsizeof(iv[0])
                 for iv in t.intervals) + sys.getsizeof(t.intervals)
    per_interval = approx / len(t.intervals)
    return {"sample_msgs": 2 * m, "intervals": len(t.intervals),
            "sample_table_bytes": approx,
            "bytes_per_interval": round(per_interval, 1),
            "extrapolated_1M_table_bytes": int(per_interval * (N // 2))}


def scenario_wal_group_commit(group=4096):
    """每 4096 条 D 记录合并一次 fsync（生产推荐用法）。"""
    path = os.path.join(HERE, "bench_wal.log")
    if os.path.exists(path):
        os.remove(path)
    with WalCommitter(path) as c:
        for start in range(0, N, group):
            c.mark_done_batch(range(start, min(start + group, N)))
    size = os.path.getsize(path)
    os.remove(path)
    return {"group": group, "wal_bytes": size}


def scenario_wal_fsync_each_sample():
    """逐条 fsync 采样 2000 条，外推 100 万条耗时。"""
    path = os.path.join(HERE, "bench_wal_sample.log")
    if os.path.exists(path):
        os.remove(path)
    m = 2000
    t0 = time.perf_counter()
    with WalCommitter(path) as c:
        for off in range(m):
            c.mark_done(off)
    dt = time.perf_counter() - t0
    os.remove(path)
    return {"sample_msgs": m, "sample_seconds": round(dt, 4),
            "extrapolated_1M_seconds": round(dt / m * N, 1)}


if __name__ == "__main__":
    rows = [
        measure("strictly_increasing(mark+advance每条)", scenario_increasing),
        measure("windowed_disorder(window=1024乱序)", scenario_windowed_disorder),
        measure("batch_mark_then_single_advance", scenario_batch_then_advance),
        measure("sparse_holes_worst_memory(50%空洞,采样外推)", scenario_sparse_holes),
        measure("wal_group_commit_4096", scenario_wal_group_commit),
        measure("wal_fsync_each_mark(采样外推)", scenario_wal_fsync_each_sample),
    ]
    out = os.path.join(HERE, "bench_results.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    print("saved:", out)

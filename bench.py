"""性能基准：1000 张表、合计 100 万行，测检测耗时与内存占用。"""
import random
import resource
import sys
import time
import tracemalloc

from staleness import StalenessDetector

N_TABLES = 1000
ROWS_PER_TABLE = 1000   # 合计 1,000,000 行


def main():
    rng = random.Random(7)
    det = StalenessDetector(rng=random.Random(8))
    now = time.time()

    # 先生成数据（不计入检测器内存）
    tables = {}
    for i in range(N_TABLES):
        rows = [rng.randint(0, 999) for _ in range(ROWS_PER_TABLE)]
        if i % 2 == 0:  # 一半表有变更
            rows += [rng.randint(0, 5999) for _ in range(rng.randint(0, 300))]
        tables[f"t{i}"] = rows

    # 建快照 + 记录变更（只计量检测器自身状态）
    tracemalloc.start()
    for i, (name, rows) in enumerate(tables.items()):
        det.record_snapshot(name, ROWS_PER_TABLE,
                            det.build_histogram(rows[:ROWS_PER_TABLE]),
                            computed_at=now - rng.random() * 30 * 86400,
                            lo=0, hi=999)
        if i % 2 == 0:
            det.record_changes(name, inserts=len(rows) - ROWS_PER_TABLE)
    snap_mem, _ = tracemalloc.get_traced_memory()

    # 1) 带分布探针的全量检测
    t0 = time.perf_counter()
    verdicts = {}
    for name, rows in tables.items():
        verdicts[name] = det.check(name, now=now, probe_rows=rows).verdict.value
    t_probe = time.perf_counter() - t0

    # 2) 无探针检测（仅计数器+时间，纯元数据路径）
    t0 = time.perf_counter()
    for name in tables:
        det.check(name, now=now)
    t_meta = time.perf_counter() - t0

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    from collections import Counter
    dist = Counter(verdicts.values())
    total_rows = sum(len(r) for r in tables.values())
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    print(f"规模: {N_TABLES} 张表, 合计 {total_rows:,} 行")
    print(f"判定分布: {dict(dist)}")
    print()
    print(f"检测耗时（带分布探针, 每表抽样 <=256 行）: {t_probe*1000:.1f} ms "
          f"({t_probe/N_TABLES*1e6:.1f} us/表)")
    print(f"检测耗时（仅计数器+时间, 不触数据）:      {t_meta*1000:.1f} ms "
          f"({t_meta/N_TABLES*1e6:.1f} us/表)")
    print()
    print(f"检测器状态内存（1000 份快照+计数器, tracemalloc）: {snap_mem/1024:.1f} KiB")
    print(f"检测全程 Python 堆峰值（tracemalloc）:             {peak/1024:.1f} KiB")
    print(f"进程峰值 RSS（含解释器与 100 万行数据本身）:        {rss_kb/1024:.1f} MiB")
    print()
    print("说明: 检测复杂度 O(表数 x 抽样数)，与全表行数无关；"
          "100 万行数据仅在抽样时被触碰。")


if __name__ == "__main__":
    main()

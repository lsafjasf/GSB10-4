"""typeinfer 性能基准：100 万行 x 1000 列。

两种工作模式：
A. 随机行抽样（默认推荐）：从 100 万行中均匀抽 SAMPLE_ROWS 行做推断。
   推断耗时与内存只取决于抽样行数，与总数据量无关。
B. 全量扫描 + 水库抽样：遍历全部行（推断本身仍只处理样本）。
   100 万行全量生成在纯 Python 下过慢，这里实测 FULL_SCAN_ROWS 行
   再按线性外推到 100 万行（生成+抽样是严格的逐行常数时间操作）。

运行: python3 benchmark.py
"""

import random
import resource
import time
import tracemalloc

from typeinfer import infer_columns

TOTAL_ROWS = 1_000_000
COLS = 1000
SAMPLE_ROWS = 2000          # 模式 A 抽样行数
FULL_SCAN_ROWS = 20_000     # 模式 B 实测行数（再外推 x50）
SEED = 42

KINDS = ["int", "float", "string", "bool", "date", "mixed", "empty"]


def gen_value(kind, rng):
    if kind == "int":
        return str(rng.randint(-10**6, 10**6))
    if kind == "float":
        return f"{rng.uniform(-1e4, 1e4):.6f}"
    if kind == "string":
        return f"name_{rng.randint(0, 10**9)}"
    if kind == "bool":
        return "true" if rng.random() < 0.5 else "false"
    if kind == "date":
        return f"2024-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    if kind == "mixed":
        return str(rng.randint(0, 100)) if rng.random() < 0.7 else f"txt_{rng.randint(0, 999)}"
    return ""  # empty


def gen_row(row_id, kinds):
    rng = random.Random(row_id)  # 按行号确定性生成，无需物化全量数据
    return [gen_value(k, rng) for k in kinds]


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def mode_a(kinds):
    """随机行抽样模式：实测 100 万行规模下的推断成本。"""
    row_ids = sorted(random.Random(SEED).sample(range(TOTAL_ROWS), SAMPLE_ROWS))
    tracemalloc.start()
    t0 = time.perf_counter()
    columns = {f"c{j}": [] for j in range(COLS)}
    for rid in row_ids:
        row = gen_row(rid, kinds)
        for j, v in enumerate(row):
            columns[f"c{j}"].append(v)
    t1 = time.perf_counter()
    results = infer_columns(columns, mixed_policy="promote", seed=7)
    t2 = time.perf_counter()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return results, t1 - t0, t2 - t1, peak / 1e6


def mode_b(kinds):
    """全量扫描模式：实测 FULL_SCAN_ROWS 行，线性外推到 100 万行。"""
    sample_rows = 500
    rng = random.Random(SEED)
    tracemalloc.start()
    t0 = time.perf_counter()
    reservoir = []
    for rid in range(FULL_SCAN_ROWS):
        row = gen_row(rid, kinds)
        if len(reservoir) < sample_rows:
            reservoir.append(row)
        else:
            j = rng.randrange(rid + 1)
            if j < sample_rows:
                reservoir[j] = row
    t1 = time.perf_counter()
    columns = {f"c{j}": [row[j] for row in reservoir] for j in range(COLS)}
    results = infer_columns(columns, mixed_policy="promote", seed=7)
    t2 = time.perf_counter()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    scale = TOTAL_ROWS / FULL_SCAN_ROWS
    return results, (t1 - t0) * scale, t2 - t1, peak / 1e6


def summarize(tag, results, gen_s, infer_s, peak_mb):
    by_status = {}
    pass_rates = []
    for r in results.values():
        by_status[r.status] = by_status.get(r.status, 0) + 1
        if r.holdout_pass_rate is not None:
            pass_rates.append(r.holdout_pass_rate)
    avg_pass = sum(pass_rates) / len(pass_rates) if pass_rates else float("nan")
    print(f"[{tag}] 列数={len(results)}")
    print(f"  数据准备/扫描耗时 : {gen_s:8.2f} s")
    print(f"  纯推断耗时        : {infer_s:8.2f} s  ({infer_s / len(results) * 1000:.2f} ms/列)")
    print(f"  Python 堆内存峰值 : {peak_mb:8.1f} MB (tracemalloc)")
    print(f"  状态分布          : {by_status}")
    print(f"  留出验证平均通过率: {avg_pass:.4f} (n={len(pass_rates)})")


def main():
    kinds = [KINDS[j % len(KINDS)] for j in range(COLS)]
    print(f"基准: {TOTAL_ROWS:,} 行 x {COLS} 列, 列类型分布={ {k: kinds.count(k) for k in KINDS} }")
    print(f"Python 3.12, 仅标准库\n")

    res_a, gen_a, infer_a, peak_a = mode_a(kinds)
    summarize(f"模式A 随机抽样 {SAMPLE_ROWS} 行 (实测)", res_a, gen_a, infer_a, peak_a)
    print()
    res_b, scan_b, infer_b, peak_b = mode_b(kinds)
    summarize(f"模式B 全量扫描 (实测 {FULL_SCAN_ROWS:,} 行, 扫描耗时已线性外推 x{TOTAL_ROWS // FULL_SCAN_ROWS})",
              res_b, scan_b, infer_b, peak_b)
    print(f"\n进程 RSS 峰值: {peak_rss_mb():.1f} MB")


if __name__ == "__main__":
    main()

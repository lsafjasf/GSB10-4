"""基准：100 万行表 + 100 种查询，对照代价模型估算与实测耗时/内存。

流程：
  1. 建表 + ANALYZE
  2. 阶段一：初始常数估算 -> 执行实测（耗时 min-of-3，内存 tracemalloc）
  3. 校准：按实测修正单位代价常数
  4. 阶段二：校准后重新估算，输出修正前后偏差对比
  5. 写 report.md 与 results.csv
"""
from __future__ import annotations

import csv
import math
import statistics
import time
import tracemalloc

from cost_model import (COMPONENTS, MEM_COMPONENTS, CostModel, Query,
                        calibrate, calibrate_mem)
from engine import Executor, Table

N_ROWS = 1_000_000
NDV = 50_000
ERR_FLOOR_MS = 0.1   # 相对偏差分母下限，避免空查询除零


def gen_queries(ndv: int) -> list[Query]:
    import random
    rng = random.Random(7)
    qs: list[Query] = []

    def add(cat, kind, lo, hi, covered, order_by, has_stats, label):
        qs.append(Query(f"{cat}{sum(1 for x in qs if x.qid.startswith(cat)):02d}",
                        kind, lo, hi, covered, order_by, has_stats, label))

    # A x20：覆盖索引足够（等值/小范围），应选 index_only
    for _ in range(20):
        v = rng.randrange(ndv)
        if rng.random() < 0.5:
            add("A", "eq", v, v, True, None, True, "覆盖索引-等值")
        else:
            w = rng.randrange(1, 20)
            add("A", "range", v, v + w, True, None, True, "覆盖索引-小范围")

    # B x20：覆盖索引范围扫描，选择率 0.01% ~ 10%
    for _ in range(20):
        sel = 10 ** rng.uniform(-4, -1)
        w = max(1, int(sel * ndv))
        lo = rng.randrange(0, ndv - w)
        add("B", "range", lo, lo + w, True, None, True, f"覆盖索引-范围sel~{sel:.2%}")

    # C x20：必须回表（投影含 payload），小选择率，应选 lookup
    for _ in range(20):
        sel = 10 ** rng.uniform(-5, -3)
        w = max(1, int(sel * ndv))
        lo = rng.randrange(0, ndv - w)
        add("C", "range", lo, lo + w, False, None, True, f"回表-小选择率sel~{sel:.2%}")

    # D x15：必须回表，大选择率，应选 full_scan
    for _ in range(15):
        sel = rng.uniform(0.05, 0.5)
        w = int(sel * ndv)
        lo = rng.randrange(0, ndv - w)
        add("D", "range", lo, lo + w, False, None, True, f"回表-大选择率sel~{sel:.0%}")

    # E x10：排序要求（索引有序可免排序 vs 需要额外排序）
    for i in range(10):
        sel = 10 ** rng.uniform(-4, -2)
        w = max(1, int(sel * ndv))
        lo = rng.randrange(0, ndv - w)
        covered = i % 2 == 0
        order_by = "k1" if i % 4 < 2 else "k2"
        add("E", "range", lo, lo + w, covered, order_by, True,
            f"排序-order by {order_by}-{'覆盖' if covered else '回表'}")

    # F x5：极端选择率
    add("F", "range", ndv + 1000, ndv + 2000, True, None, True, "极端-空范围")
    add("F", "eq", 0, 0, False, None, True, "极端-单行回表")
    add("F", "range", 0, ndv // 2, True, None, True, "极端-50%覆盖")
    add("F", "range", 0, ndv, True, None, True, "极端-100%覆盖")
    add("F", "range", 0, ndv, False, None, True, "极端-100%回表")

    # G x10：统计信息缺失 -> 无法估算，保守选择
    for i in range(10):
        sel = 10 ** rng.uniform(-4, -2)
        w = max(1, int(sel * ndv))
        lo = rng.randrange(0, ndv - w)
        covered = i % 2 == 0
        add("G", "range", lo, lo + w, covered, None, False,
            "统计缺失-" + ("覆盖" if covered else "非覆盖"))
    return qs


def rel_err(est: float, actual: float) -> float:
    return abs(est - actual) / max(actual, ERR_FLOOR_MS)


def timed_execute(ex: Executor, plan: str, q: Query, reps: int = 3):
    best, res = math.inf, None
    for _ in range(reps):
        t0 = time.perf_counter()
        res = ex.execute(plan, q)
        best = min(best, (time.perf_counter() - t0) * 1000.0)
    return best, res


def mem_execute(ex: Executor, plan: str, q: Query) -> int:
    tracemalloc.start()
    ex.execute(plan, q)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(p * len(values)))]


def main() -> None:
    t0 = time.perf_counter()
    table = Table(N_ROWS, NDV)
    stats = table.analyze()
    print(f"建表 {N_ROWS:,} 行 + ANALYZE 完成，耗时 {time.perf_counter() - t0:.1f}s，"
          f"NDV={stats.ndv:,}，堆页={table.heap_pages:,}")

    queries = gen_queries(NDV)
    assert len(queries) == 100
    model = CostModel(N_ROWS)
    ex = Executor(table)

    rows_out = []
    records = []  # (units, actual_ms) 供校准
    for q in queries:
        st = stats if q.has_stats else None
        est, _ = model.choose(q, st)
        actual_ms, res = timed_execute(ex, est.plan, q)
        peak_mem = mem_execute(ex, est.plan, q)
        truth = table.actual_count(q)
        assert res.rows == truth, f"{q.qid} 行数不符: {res.rows} != {truth}"
        if est.feasible:
            assert abs(est.est_rows - truth) / max(truth, 1) < 0.5 or truth < 100, \
                f"{q.qid} 行数估算偏差异常: est={est.est_rows:.0f} actual={truth}"
            records.append((est.units, actual_ms))
        rows_out.append(dict(q=q, est=est, actual_ms=actual_ms, peak_mem=peak_mem,
                             rows=res.rows, seq=res.seq_pages, rand=res.rand_pages,
                             sort=res.sort_rows))

    # ---- 校准 ----
    c2 = calibrate(model.c, records, rounds=8)
    mem_records = [(r["est"].mem_units, float(r["peak_mem"]))
                   for r in rows_out if r["est"].feasible and r["peak_mem"] > 0]
    c2 = calibrate_mem(c2, mem_records, rounds=8)
    model2 = CostModel(N_ROWS, c2)
    for r in rows_out:
        if r["est"].feasible:
            r["est2"] = model2.estimate_plan(r["est"].plan, r["q"], stats)
        else:
            r["est2"] = r["est"]

    feas = [r for r in rows_out if r["est"].feasible]
    err_b = [rel_err(r["est"].total_ms, r["actual_ms"]) for r in feas]
    err_a = [rel_err(r["est2"].total_ms, r["actual_ms"]) for r in feas]
    mem_err_b = [abs(r["est"].mem_bytes - r["peak_mem"]) / max(r["peak_mem"], 1)
                 for r in feas]
    mem_err_a = [abs(r["est2"].mem_bytes - r["peak_mem"]) / max(r["peak_mem"], 1)
                 for r in feas]

    def stat_line(es):
        return (f"均值 {statistics.mean(es) * 100:6.1f}%  中位 {statistics.median(es) * 100:6.1f}%  "
                f"P95 {pct(es, 0.95) * 100:6.1f}%  最大 {max(es) * 100:6.1f}%  "
                f">25% 的查询数 {sum(1 for e in es if e > 0.25)}/{len(es)}")

    print("\n== 单位代价常数（校准前 -> 校准后）==")
    for n in COMPONENTS:
        print(f"  {n:10s} {model.c.price(n):.6f} -> {c2.price(n):.6f} ms")
    print("\n== 估算耗时相对偏差 ==")
    print("校准前:", stat_line(err_b))
    print("校准后:", stat_line(err_a))

    mem_ratios = [r["peak_mem"] / max(r["est2"].mem_bytes, 1) for r in feas]
    print(f"\n== 内存相对偏差 ==  校准前 中位 {statistics.median(mem_err_b)*100:.1f}%  "
          f"校准后 中位 {statistics.median(mem_err_a)*100:.1f}%")
    print(f"实测/估算中位比 {statistics.median(mem_ratios):.2f}，"
          f"100 查询实测峰值合计 {sum(r['peak_mem'] for r in rows_out) / 1e6:.1f} MB")

    # ---- results.csv ----
    with open("results.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["qid", "label", "plan", "feasible", "reason", "rows_est",
                    "rows_actual", "est_ms_before", "est_ms_after", "actual_ms",
                    "err_before_pct", "err_after_pct", "est_mem_before_bytes",
                    "est_mem_after_bytes", "measured_mem_bytes",
                    "mem_err_before_pct", "mem_err_after_pct",
                    "seq_pages", "rand_pages", "sort_rows"])
        for r in rows_out:
            e, e2 = r["est"], r["est2"]
            w.writerow([r["q"].qid, r["q"].label, e.plan, e.feasible, e.reason,
                        round(e.est_rows, 1) if e.feasible else "",
                        r["rows"],
                        round(e.total_ms, 4) if e.feasible else "",
                        round(e2.total_ms, 4) if e2.feasible else "",
                        round(r["actual_ms"], 4),
                        round(rel_err(e.total_ms, r["actual_ms"]) * 100, 1) if e.feasible else "",
                        round(rel_err(e2.total_ms, r["actual_ms"]) * 100, 1) if e2.feasible else "",
                        int(e.mem_bytes) if e.feasible else "",
                        int(e2.mem_bytes) if e2.feasible else "",
                        r["peak_mem"],
                        round(abs(e.mem_bytes - r["peak_mem"]) / max(r["peak_mem"], 1) * 100, 1) if e.feasible else "",
                        round(abs(e2.mem_bytes - r["peak_mem"]) / max(r["peak_mem"], 1) * 100, 1) if e2.feasible else "",
                        r["seq"], r["rand"], r["sort"]])

    # ---- report.md ----
    cats = {}
    for r in rows_out:
        cats.setdefault(r["q"].qid[0], []).append(r)
    cat_names = {"A": "覆盖索引足够", "B": "覆盖索引范围扫描", "C": "必须回表-小选择率",
                 "D": "必须回表-大选择率", "E": "排序要求", "F": "极端选择率",
                 "G": "统计信息缺失"}
    lines = ["# 基准报告（100 万行 x 100 种查询）", "",
             f"- 表：{N_ROWS:,} 行，NDV={stats.ndv:,}，堆页={table.heap_pages:,}，索引叶页≈{model.index_pages:,}",
             f"- 耗时为 3 次执行取最小；内存为 tracemalloc 单次峰值", "",
             "## 单位代价常数校准", "",
             "| 分量 | 校准前 (ms) | 校准后 (ms) |", "|---|---|---|"]
    for n in COMPONENTS:
        lines.append(f"| {n} | {model.c.price(n):.6f} | {c2.price(n):.6f} |")
    lines += ["", "| 内存分量 (B/单位) | 校准前 | 校准后 |", "|---|---|---|"]
    for n in MEM_COMPONENTS:
        lines.append(f"| {n} | {model.c.unit(n):.2f} | {c2.unit(n):.2f} |")
    lines += ["", "## 估算耗时相对偏差（|估-实|/实）", "",
              "| 阶段 | 均值 | 中位 | P95 | 最大 | 偏差>25% 查询数 |", "|---|---|---|---|---|---|"]
    for name, es in (("校准前", err_b), ("校准后", err_a)):
        lines.append(f"| {name} | {statistics.mean(es)*100:.1f}% | {statistics.median(es)*100:.1f}% "
                     f"| {pct(es,0.95)*100:.1f}% | {max(es)*100:.1f}% "
                     f"| {sum(1 for e in es if e>0.25)}/{len(es)} |")
    lines += ["", "## 分类汇总", "",
              "| 类别 | 查询数 | 计划 | 平均实测 ms | 校准前平均偏差 | 校准后平均偏差 |",
              "|---|---|---|---|---|---|"]
    for c, rs in sorted(cats.items()):
        plans = "/".join(sorted({r["est"].plan for r in rs}))
        frs = [r for r in rs if r["est"].feasible]
        avg_ms = statistics.mean([r["actual_ms"] for r in rs])
        if frs:
            eb = statistics.mean([rel_err(r["est"].total_ms, r["actual_ms"]) for r in frs])
            ea = statistics.mean([rel_err(r["est2"].total_ms, r["actual_ms"]) for r in frs])
            lines.append(f"| {c} {cat_names[c]} | {len(rs)} | {plans} | {avg_ms:.3f} "
                         f"| {eb*100:.1f}% | {ea*100:.1f}% |")
        else:
            lines.append(f"| {c} {cat_names[c]} | {len(rs)} | {plans} | {avg_ms:.3f} | - | - |")
    lines += ["", "## 统计信息缺失的查询（显式无法估算 -> 保守选择）", "",
              "| qid | 场景 | 保守计划 | 原因 |", "|---|---|---|---|"]
    for r in rows_out:
        if not r["est"].feasible:
            lines.append(f"| {r['q'].qid} | {r['q'].label} | {r['est'].plan} | {r['est'].reason} |")
    lines += ["", "## 内存占用", "",
              "- 估算内存 = 排序行数*sort_row_bytes + 缓存页数*cache_entry_bytes + fixed_bytes",
              f"- 相对偏差：校准前中位 {statistics.median(mem_err_b)*100:.1f}% "
              f"-> 校准后中位 {statistics.median(mem_err_a)*100:.1f}%",
              f"- 实测/估算中位比：{statistics.median(mem_ratios):.2f}",
              f"- 100 查询实测峰值合计：{sum(r['peak_mem'] for r in rows_out)/1e6:.1f} MB",
              "", "明细见 results.csv。"]
    with open("report.md", "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n已写出 report.md 与 results.csv")


if __name__ == "__main__":
    main()

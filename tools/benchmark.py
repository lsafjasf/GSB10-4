"""耗时基准：
1) 100 万行量级的并/交/差（左 50 万 + 右 50 万，共 100 万行）；
2) 1000+ 种类型组合扫描（有序类型对 x 运算 x 空值模式 = 1296 种），
   每种 1000 行/侧；可提升的计时，不可比的验证拒绝率。
"""
import os
import platform
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from setops import Dataset, Type, join
from tools.pools import sample_value

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "benchmark_results.md")


def timed(fn, *args):
    t0 = time.perf_counter()
    result = fn(*args)
    return time.perf_counter() - t0, result


def bench_one_million():
    """左 50 万 int64（含 1% 超 2^53 值）+ 右 50 万 float64，总计 100 万行。"""
    rng = random.Random(1)
    n = 500_000

    def int64_row():
        if rng.random() < 0.01:
            return (2 ** 53 + rng.randint(1, 10_000_000),)
        return (rng.randint(0, 2_000_000),)

    def float_row():
        if rng.random() < 0.5:
            return (float(rng.randint(0, 2_000_000)),)  # 与左侧大量重合
        return (rng.uniform(0, 10_000),)

    build_t0 = time.perf_counter()
    left = Dataset([Type.INT64], (int64_row() for _ in range(n)),
                   validate_values=False)
    right = Dataset([Type.FLOAT64], (float_row() for _ in range(n)),
                    validate_values=False)
    build_t = time.perf_counter() - build_t0

    out = []
    for op in ("union", "intersection", "difference"):
        t, r = timed(getattr(left, op), right)
        out.append({
            "op": op,
            "seconds": t,
            "result_rows": len(r.rows),
            "precision_loss": r.precision.total_loss_count,
            "rows_per_s": int(1_000_000 / t),
        })

    # 同类型对照：100 万行 string
    words = [f"w{i}" for i in range(2000)]
    ls = Dataset([Type.STRING], ((rng.choice(words),) for _ in range(n)),
                 validate_values=False)
    rs = Dataset([Type.STRING], ((rng.choice(words),) for _ in range(n)),
                 validate_values=False)
    t_s, rs_union = timed(ls.union, rs)

    # 三列混合 100 万行交集（int32 + string + date）
    from datetime import date, timedelta
    small_words = words[:20]
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(5)]
    from datetime import datetime
    def multi_rows():
        return ((rng.randint(0, 20), rng.choice(small_words), rng.choice(dates))
                for _ in range(n))
    def multi_rows_dt():
        def stamp():
            d = rng.choice(dates)
            # 70% 午夜（提升后与左侧 date 相等），30% 随机小时
            if rng.random() < 0.7:
                return datetime(d.year, d.month, d.day)
            return datetime(d.year, d.month, d.day, rng.randint(1, 23))
        return ((rng.randint(0, 20), rng.choice(small_words), stamp())
                for _ in range(n))
    lm = Dataset([Type.INT32, Type.STRING, Type.DATE], multi_rows(),
                 validate_values=False)
    rm = Dataset([Type.INT64, Type.STRING, Type.DATETIME], multi_rows_dt(),
                 validate_values=False)
    t_m, r_m = timed(lm.intersection, rm)
    return build_t, out, t_s, len(rs_union.rows), t_m, len(r_m.rows)


def bench_combos():
    types = list(Type)  # 12 种
    ops = ("union", "intersection", "difference")
    null_modes = ("none", "left", "both")
    rows_per_side = 1000
    combos = []
    comparable_times = []
    rejected = 0
    rng = random.Random(2)
    for a in types:
        for b in types:
            comparable = join(a, b) is not None
            for op in ops:
                for mode in null_modes:
                    combos.append((a, b, op, mode, comparable))
    t0 = time.perf_counter()
    total_rows = 0
    for a, b, op, mode, comparable in combos:
        left_rows = [(None,) if (mode in ("left", "both") and rng.random() < 0.1)
                     else (sample_value(a, rng),) for _ in range(rows_per_side)]
        right_rows = [(None,) if (mode == "both" and rng.random() < 0.1)
                      else (sample_value(b, rng),) for _ in range(rows_per_side)]
        total_rows += 2 * rows_per_side
        da = Dataset([a], left_rows, validate_values=False)
        db = Dataset([b], right_rows, validate_values=False)
        t1 = time.perf_counter()
        if comparable:
            getattr(da, op)(db)
            comparable_times.append(time.perf_counter() - t1)
        else:
            try:
                getattr(da, op)(db)
            except Exception:
                rejected += 1
            else:
                raise AssertionError(f"{a} vs {b} 应被拒绝")
    total = time.perf_counter() - t0
    return {
        "combo_count": len(combos),
        "comparable": len(comparable_times),
        "rejected": rejected,
        "rows_processed": total_rows,
        "total_seconds": total,
        "mean_us": statistics.mean(comparable_times) * 1e6,
        "median_us": statistics.median(comparable_times) * 1e6,
        "p95_us": statistics.quantiles(comparable_times, n=20)[18] * 1e6,
        "max_us": max(comparable_times) * 1e6,
    }


def main():
    build_t, million, t_s, s_rows, t_m, m_rows = bench_one_million()
    sweep = bench_combos()

    lines = [
        "# 耗时数据",
        "",
        f"- Python: {platform.python_version()} on {platform.platform()}",
        f"- 时间点: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "- 纯标准库、单进程实现；时间为 `time.perf_counter()` 实测。",
        "",
        "## 1. 100 万行（左 50 万 int64 + 右 50 万 float64，含精度检测）",
        "",
        f"- 数据集构造（100 万行）: {build_t:.3f} s",
        "",
        "| 运算 | 耗时 (s) | 吞吐 (行/s) | 结果行数 | 精度丢失值数 |",
        "|---|---|---|---|---|",
    ]
    for r in million:
        lines.append(
            f"| {r['op']} | {r['seconds']:.3f} | {r['rows_per_s']:,} | "
            f"{r['result_rows']:,} | {r['precision_loss']:,} |"
        )
    lines += [
        "",
        f"同类型对照（100 万行 string union）: {t_s:.3f} s，"
        f"结果 {s_rows:,} 行，吞吐 {int(1_000_000 / t_s):,} 行/s。",
        "",
        f"三列混合（100 万行 int32+string+date vs int64+string+datetime 交集）: "
        f"{t_m:.3f} s，结果 {m_rows:,} 行，吞吐 {int(1_000_000 / t_m):,} 行/s。",
        "",
        f"## 2. {sweep['combo_count']} 种类型组合扫描",
        "",
        f"- 组合构成: 12 种类型的有序类型对（{12*12}）x 3 种运算 x 3 种空值模式 "
        f"= {sweep['combo_count']} 种（≥ 1000）",
        f"- 每种组合每侧 {1000} 行，共处理 {sweep['rows_processed']:,} 行",
        f"- 可提升组合: {sweep['comparable']} 个；不可比拒绝: {sweep['rejected']} 个（0 漏放）",
        f"- 总耗时: {sweep['total_seconds']:.3f} s，"
        f"单组合均摊 {sweep['total_seconds']/sweep['combo_count']*1000:.2f} ms",
        "",
        "| 指标（仅可提升组合的单次运算） | 值 |",
        "|---|---|",
        f"| 平均耗时 | {sweep['mean_us']:.0f} µs |",
        f"| 中位数 | {sweep['median_us']:.0f} µs |",
        f"| p95 | {sweep['p95_us']:.0f} µs |",
        f"| 最大 | {sweep['max_us']:.0f} µs |",
        "",
    ]
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))
    print(f"已生成 {OUT}")


if __name__ == "__main__":
    main()

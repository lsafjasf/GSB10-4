"""基准：100 万行表 + 100 种查询，输出估算/实测耗时与内存、修正前后偏差对比。

运行：python3 benchmark.py
"""

import random
import statistics

from cost_model import (CostModel, Engine, Query, calibrate)

N = 1_000_000
DISTINCT = 10_000
ROW_WIDTH = 200


def gen_queries(rng):
    """100 种查询：覆盖/回表/排序/极端选择率/统计缺失 混合。"""
    qs = []

    def rand_range(max_w_exp=4):
        w = 10 ** rng.uniform(1, max_w_exp)
        lo = rng.uniform(0, DISTINCT - min(w, DISTINCT - 1))
        return lo, lo + w

    def add(kind, order=False, stats=True, lo=None, hi=None):
        covering = kind == "cov"
        if lo is None:
            lo, hi = rand_range(2 if order else 4)
        qs.append((Query(lo=lo, hi=hi, covering=covering, order_by_index=order,
                         proj_width=24 if covering else ROW_WIDTH,
                         name="Q%03d" % len(qs)), stats))

    for _ in range(25):
        add("cov")
    for _ in range(35):
        add("ncov")
    for _ in range(10):
        add("cov", order=True)
    for _ in range(10):
        add("ncov", order=True)
    for _ in range(5):                                   # 极端：空范围
        add("cov", lo=DISTINCT + 1000, hi=DISTINCT + 2000)
    for _ in range(5):                                   # 极端：全表范围
        add("ncov", lo=-1, hi=DISTINCT + 1)
    for i in range(10):                                  # 统计信息缺失
        add("cov" if i % 2 == 0 else "ncov", stats=False)
    assert len(qs) == 100
    return qs


def dev(est, act):
    return abs(est - act) / max(act, 100.0)              # 100us 下限防空结果除零


def mem_dev(est, act):
    return abs(est - act) / max(act, 4096.0)             # 1 页下限防除零


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def main():
    rng = random.Random(42)
    print("构建 100 万行表与索引 ...")
    rows = [(rng.randrange(DISTINCT), rng.random(), "p" * (ROW_WIDTH - 64))
            for _ in range(N)]
    eng = Engine(rows, ROW_WIDTH)
    stats = eng.build_stats()
    print("统计信息: rows=%d width=%dB pages=%d distinct=%d clustering=%.3f"
          % (stats.row_count, stats.row_width, stats.num_pages,
             stats.n_distinct, stats.clustering))

    model = CostModel()
    queries = gen_queries(rng)
    samples, records = [], []

    hdr = ("%-4s %-6s %-4s %-14s %10s %10s %8s %10s %10s %8s"
           % ("id", "类型", "排序", "计划", "估算ms", "实测ms", "偏差%",
              "估算MB", "实测MB", "内存比"))
    print("\n" + hdr)
    print("-" * len(hdr))
    for q, has_stats in queries:
        dec = model.choose(q, stats if has_stats else None)
        res = eng.run(dec.plan, q)
        samples.append((res.features, res.time_us))
        if dec.estimated:
            est = dec.estimates[dec.plan]
            est_ms, est_mb = est.est_us / 1e3, est.est_mem / 1e6
            d, mr = dev(est.est_us, res.time_us), res.mem_bytes / max(1.0, est.est_mem)
            records.append((q, dec, res, est.est_us, est.est_mem))
            print("%-4s %-6s %-4s %-14s %10.2f %10.2f %8.1f %10.3f %10.3f %8.2f"
                  % (q.name, "覆盖" if q.covering else "回表?",
                     "是" if q.order_by_index else "否", dec.plan,
                     est_ms, res.time_us / 1e3, d * 100,
                     est_mb, res.mem_bytes / 1e6, mr))
        else:
            print("%-4s %-6s %-4s %-14s %10s %10.2f %8s %10s %10.3f %8s"
                  % (q.name, "覆盖" if q.covering else "回表?",
                     "是" if q.order_by_index else "否", dec.plan,
                     "无法估算", res.time_us / 1e3, "-", "-",
                     res.mem_bytes / 1e6, "-"))
            print("      ^-- %s" % dec.reason)

    # ---- 修正前偏差 ----
    before = [dev(e, r.time_us) for _, _, r, e, _ in records]
    mem_before = [mem_dev(em, r.mem_bytes) for _, _, r, _, em in records]

    # ---- 反馈修正：最小二乘重估代价常数 + 内存系数 ----
    tuned_const = calibrate(samples)
    mem_factor = statistics.median(
        r.mem_bytes / em for _, _, r, _, em in records if em > 4096)
    tuned = CostModel(tuned_const, mem_factor=mem_factor)

    after, mem_after = [], []
    for q, dec, res, _e, _em in records:
        dec2 = tuned.choose(q, stats)
        est2 = dec2.estimates[dec2.plan]
        after.append(dev(est2.est_us, res.time_us))
        mem_after.append(mem_dev(est2.est_mem, res.mem_bytes))

    print("\n==== 耗时估算偏差（|估-实|/实，越小越好）====")
    print("修正前: mean=%.1f%% median=%.1f%% p90=%.1f%%"
          % (statistics.mean(before) * 100, statistics.median(before) * 100,
             pct(before, 0.9) * 100))
    print("修正后: mean=%.1f%% median=%.1f%% p90=%.1f%%"
          % (statistics.mean(after) * 100, statistics.median(after) * 100,
             pct(after, 0.9) * 100))
    print("修正后代价常数: %s" % tuned_const)
    print("内存修正系数(中位数): %.3f" % mem_factor)
    print("内存估算偏差: 修正前 mean=%.1f%% median=%.1f%%  修正后 mean=%.1f%% median=%.1f%%"
          % (statistics.mean(mem_before) * 100, statistics.median(mem_before) * 100,
             statistics.mean(mem_after) * 100, statistics.median(mem_after) * 100))

    # ---- 决策质量抽查：与所有候选计划的实测最优对比 ----
    print("\n==== 决策质量抽查（与实测最优计划对比）====")
    checked = good_before = good_after = 0
    for q, dec, res, _e, _em in records:
        est_rows = dec.estimates[dec.plan].features["out_rows"]
        if est_rows > 300_000:          # 太大的计划全部跑一遍太慢，跳过
            continue
        actuals = {p: eng.run(p, q).time_us
                   for p in model.feasible_plans(q)}
        best = min(actuals.values())
        dec2 = tuned.choose(q, stats)
        checked += 1
        if actuals[dec.plan] <= 1.2 * best:
            good_before += 1
        if actuals[dec2.plan] <= 1.2 * best:
            good_after += 1
    print("抽查 %d 条，所选计划实测耗时 <= 1.2x 实测最优：" % checked)
    print("  修正前 %d/%d (%.0f%%)   修正后 %d/%d (%.0f%%)"
          % (good_before, checked, 100.0 * good_before / max(1, checked),
             good_after, checked, 100.0 * good_after / max(1, checked)))


if __name__ == "__main__":
    main()

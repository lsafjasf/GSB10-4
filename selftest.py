"""自测：模拟工作负载 -> 检测 -> 重算统计做真值对照 -> 误判/漏判/阈值扫描。

真值定义（重算统计后的实际差异）：
  actual_row_change = |新行数 - 快照行数| / 快照行数
  actual_drift      = max(全量TV距离, 全量越界率) （与检测端信号定义一致）
  真值 STALE 当且仅当 actual_row_change > EPS_ROW 或 actual_drift > EPS_DRIFT
"""
import random
import time
from collections import Counter
from dataclasses import replace

from staleness import StalenessDetector, Thresholds, Verdict

EPS_ROW = 0.10    # 真值判定：行数变化超过 10% 视为实际过期
EPS_DRIFT = 0.10  # 真值判定：分布漂移超过 0.10 视为实际过期

SCENARIOS = ["never", "micro", "moderate", "drastic", "missing"]


def gen_rows(rng, n, lo, hi):
    return [rng.randint(lo, hi) for _ in range(n)]


def simulate(seed=42, tables_per_scenario=40, rows_per_table=1000,
             n_mixed=200):
    """五组固定场景 + 一组参数连续随机的 mixed 表（用于阈值权衡分析）。"""
    rng = random.Random(seed)
    det = StalenessDetector(rng=random.Random(seed + 1))
    now = time.time()
    tables = {}

    def add_table(name, rows, age_days, scenario, n_add, n_del):
        if scenario != "missing":
            det.record_snapshot(
                name, rows_per_table, det.build_histogram(rows),
                computed_at=now - age_days * 86400.0,
                lo=0, hi=999)
            det.record_changes(name, inserts=n_add, deletes=n_del)
        tables[name] = {"rows": rows, "scenario": scenario}

    for scenario in SCENARIOS:
        for i in range(tables_per_scenario):
            name = f"{scenario}_{i}"
            base = gen_rows(rng, rows_per_table, 0, 999)
            if scenario == "missing":
                tables[name] = {"rows": base, "scenario": "missing"}
            elif scenario == "never":
                add_table(name, list(base), 10.0, scenario, 0, 0)
            elif scenario == "micro":
                k = max(1, int(0.005 * rows_per_table))
                rows = list(base)
                for _ in range(k):
                    rows[rng.randrange(len(rows))] = rng.randint(0, 999)
                add_table(name, rows, 10.0, scenario, 0, 0)
                det.record_changes(name, updates=k)
            elif scenario == "moderate":
                # 净增 25% 行（新增 30% + 删除 5%），同分布
                n_add, n_del = int(0.30 * rows_per_table), int(0.05 * rows_per_table)
                rows = list(base) + gen_rows(rng, n_add, 0, 999)
                del rows[:n_del]
                add_table(name, rows, 10.0, scenario, n_add, n_del)
            elif scenario == "drastic":
                n_add = n_del = int(0.5 * rows_per_table)  # 净行数不变
                rows = list(base) + gen_rows(rng, n_add, 5000, 5999)
                del rows[:n_del]
                add_table(name, rows, 10.0, scenario, n_add, n_del)

    for i in range(n_mixed):
        name = f"mixed_{i}"
        base = gen_rows(rng, rows_per_table, 0, 999)
        rows = list(base)
        frac = rng.random() * 0.6                    # churn 比例 0~60%
        n_add = int(frac * rows_per_table * rng.random())
        n_del = int(frac * rows_per_table * rng.random())
        shifted = rng.random() < rng.random()        # 约一半概率值域迁移
        rows += gen_rows(rng, n_add, 5000, 5999) if shifted \
            else gen_rows(rng, n_add, 0, 999)
        del rows[:n_del]
        add_table(name, rows, rng.random() * 60.0, "mixed", n_add, n_del)

    return det, tables, now


def ground_truth(det, name, rows):
    snap = det.snapshots[name]
    new_hist = det.build_histogram(rows)
    arc = abs(len(rows) - snap.row_count) / max(snap.row_count, 1)
    tv_out = 0.0
    if rows:
        tv_out = sum(1 for v in rows
                     if v < snap.lo or v > snap.hi) / len(rows)
    adrift = max(__import__("staleness").total_variation(snap.histogram, new_hist),
                 tv_out)
    return (arc > EPS_ROW or adrift > EPS_DRIFT), arc, adrift


def evaluate(det, tables, now, thresholds=None, only_scenario=None):
    if thresholds is not None:
        det.th = thresholds
    tp = fp = tn = fn = unknown = 0
    per = {}
    for name, info in tables.items():
        sc = info["scenario"]
        if only_scenario and sc != only_scenario:
            continue
        d = det.check(name, now=now, probe_rows=info["rows"])
        s = per.setdefault(sc, {"tp":0,"fp":0,"tn":0,"fn":0,"unknown":0})
        if d.verdict is Verdict.UNKNOWN:
            unknown += 1; s["unknown"] += 1
            continue
        actual, _, _ = ground_truth(det, name, info["rows"])
        pred = d.verdict is Verdict.STALE
        if pred and actual: tp += 1; s["tp"] += 1
        elif pred: fp += 1; s["fp"] += 1
        elif actual: fn += 1; s["fn"] += 1
        else: tn += 1; s["tn"] += 1
    judged = tp + fp + tn + fn
    return dict(tp=tp, fp=fp, tn=tn, fn=fn, unknown=unknown, judged=judged,
                fp_rate=fp / max(fp + tn, 1),
                fn_rate=fn / max(fn + tp, 1),
                recomputes=tp + fp, per_scenario=per)


def main():
    det, tables, now = simulate()
    cnt = Counter(t["scenario"] for t in tables.values())
    print("模拟规模:", len(tables), "张表 ("
          + ", ".join(f"{k}={v}" for k, v in sorted(cnt.items())) + ")")
    print(f"真值标准: 行变比>{EPS_ROW:.0%} 或 漂移>{EPS_DRIFT} 即实际过期\n")

    print("== 五类要求情形：逐表信号（每类取第 0 张）==")
    print(f"  {'场景':9s} {'变更量':>7s} {'TV/越界漂移':>11s} {'年龄天':>7s} "
          f"{'分数':>6s} {'判定':>8s} {'真值':>6s}")
    for sc in SCENARIOS:
        name = f"{sc}_0"
        d = det.check(name, now=now, probe_rows=tables[name]["rows"])
        if d.verdict is Verdict.UNKNOWN:
            truth = "-"
        else:
            truth = "过期" if ground_truth(det, name, tables[name]["rows"])[0] else "新鲜"
        sig = d.signals
        print(f"  {sc:9s} "
              f"{sig['change_ratio'] if sig['change_ratio'] is not None else float('nan'):7.3f} "
              f"{sig['drift'] if sig['drift'] is not None else float('nan'):11.3f} "
              f"{sig['age_days'] if sig['age_days'] is not None else float('nan'):7.1f} "
              f"{d.score if d.score is not None else float('nan'):6.3f} "
              f"{d.verdict.value:>8s} {truth:>6s}")

    base = evaluate(det, tables, now)
    print("\n== 默认阈值：全部表判定 vs 重算真值 ==")
    print(f"  TP={base['tp']} FP={base['fp']} TN={base['tn']} "
          f"FN={base['fn']} UNKNOWN={base['unknown']}")
    print(f"  误判率(FP率)={base['fp_rate']:.2%}  漏判率(FN率)={base['fn_rate']:.2%} "
          f" 触发重算={base['recomputes']}/{base['judged']}")
    for sc in SCENARIOS + ["mixed"]:
        s = base["per_scenario"].get(sc, {})
        print(f"    {sc:8s} tp={s.get('tp',0):3d} fp={s.get('fp',0):3d} "
              f"tn={s.get('tn',0):3d} fn={s.get('fn',0):3d} "
              f"unknown={s.get('unknown',0):3d}")

    m = base["per_scenario"]["missing"]
    n_missing = cnt["missing"]
    assert m["unknown"] == n_missing and m["tp"] == m["fp"] == m["tn"] == m["fn"] == 0
    print("\n  [断言通过] 统计信息缺失的表全部 UNKNOWN，无一默认新鲜")

    def sweep(label, field_, values, fmt=".2f"):
        print(f"\n== 阈值扫描：{label}（mixed={cnt['mixed']} 张连续参数表）==")
        print(f"  {label:>8s} {'误判率':>8s} {'漏判率':>8s} {'重算次数':>8s}")
        for v in values:
            r = evaluate(det, tables, now,
                         thresholds=replace(Thresholds(), **{field_: v}),
                         only_scenario="mixed")
            print(f"  {v:8{fmt}} {r['fp_rate']:8.2%} {r['fn_rate']:8.2%} "
                  f"{r['recomputes']:8d}")

    sweep("综合分阈值", "score", (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8))
    sweep("变更量阈值", "change_ratio", (0.05, 0.10, 0.20, 0.30, 0.50))
    sweep("漂移阈值", "drift", (0.10, 0.15, 0.25, 0.35, 0.50))
    sweep("时间阈值(天)", "age_days", (7, 14, 30, 60, 120), fmt=".0f")

    print("\nSELFTEST OK")


if __name__ == "__main__":
    main()

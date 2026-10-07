"""统计信息过期检测 —— 自测 / 对照 / 阈值扫描 / 性能（仅标准库）。

运行：  python3 selftest.py
"""

from __future__ import annotations

import random
import sys
import time
import tracemalloc
from array import array

from staleness import (UNKNOWN, Detector, Histogram, Signals, Snapshot, TableState,
    Thresholds, psi)

NOW = 50                 # 当前逻辑时钟
ROWS = 1000              # 每表行数
TABLES = 1000            # 总表数（1000 x 1000 = 100 万行）
TRUTH_PSI = 0.10         # 重算后真实漂移的认定界（PSI 经验下界）
TRUTH_ROWCHG = 0.10      # 行数真实变化 >=10% 视为统计失真

# 场景占比（合计 1000）
SCENARIOS = (["never"] * 400      # 从未变更
             + ["micro"] * 200    # 微变：0.5% 同分布更新
             + ["churn"] * 150    # 高换血率但分布不变
             + ["moderate"] * 50  # 中等分布漂移
             + ["drastic"] * 150  # 剧变：分布大幅平移+扩宽
             + ["missing"] * 50)  # 统计信息缺失


def build_tables(seed=42):
    rng = random.Random(seed)
    out = []
    for i, scenario in enumerate(SCENARIOS):
        data = array("d", (rng.gauss(100.0, 15.0) for _ in range(ROWS)))
        tbl = TableState(name=f"t{i:04d}", sample_cap=128)
        taken_at = rng.randrange(0, 46)   # 快照年龄 5..50
        if scenario != "missing":
            tbl.snapshot = Snapshot(taken_at, len(data), Histogram(data))

        if scenario == "micro":
            for idx in rng.sample(range(ROWS), 5):
                val = rng.gauss(100.0, 15.0)
                data[idx] = val
                tbl.updates += 1
                tbl.record_write(val, rng)
        elif scenario == "churn":
            data, removed = _delete(data, rng, 300)
            tbl.deletes += removed
            for _ in range(300):
                val = rng.gauss(100.0, 15.0)
                data.append(val)
                tbl.inserts += 1
                tbl.record_write(val, rng)
        elif scenario == "moderate":
            for idx in rng.sample(range(ROWS), 100):
                val = rng.gauss(130.0, 20.0)
                data[idx] = val
                tbl.updates += 1
                tbl.record_write(val, rng)
        elif scenario == "drastic":
            data, removed = _delete(data, rng, 400)
            tbl.deletes += removed
            for _ in range(400):
                val = rng.gauss(160.0, 30.0)
                data.append(val)
                tbl.inserts += 1
                tbl.record_write(val, rng)
        elif scenario == "missing":
            for _ in range(100):
                val = rng.gauss(120.0, 20.0)
                data.append(val)
                tbl.inserts += 1
                tbl.record_write(val, rng)
        out.append((scenario, tbl, data))
    return out


def _delete(data: array, rng: random.Random, n: int):
    """从数组中删除 n 个随机位置（从后向前交换删除），返回新数组与删除数。"""
    keep = len(data) - n
    for _ in range(n):
        j = rng.randrange(len(data))
        data[j], data[-1] = data[-1], data[j]
        data.pop()
    return data, n


def ground_truth(tbl: TableState, data) -> tuple[float, float]:
    """重算统计：全量数据 vs 快照，返回 (实际 PSI, 行数变化率)。"""
    snap = tbl.snapshot
    assert snap is not None
    actual_frac = snap.hist.fractions_of(data)
    actual_psi = psi(snap.hist.frac, actual_frac)
    rowchg = abs(len(data) - snap.row_count) / snap.row_count
    return actual_psi, rowchg


def is_truly_stale(t: float, rowchg: float) -> bool:
    return t >= TRUTH_PSI or rowchg >= TRUTH_ROWCHG


def evaluate(tables, th: Thresholds):
    """返回 (混淆矩阵, 每场景明细, 信号缓存)。UNKNOWN 单独统计。"""
    det = Detector(th)
    tp = fp = tn = fn = unknown = 0
    per = {s: {"total": 0, "FRESH": 0, "STALE": 0, "UNKNOWN": 0,
               "fp": 0, "fn": 0, "true_stale": 0} for s in
           ("never", "micro", "churn", "moderate", "drastic", "missing")}
    cached = []
    for scenario, tbl, data in tables:
        cached.append(tbl)
        verdict = det.assess(tbl, NOW)
        p = per[scenario]
        p["total"] += 1
        p[verdict.status] += 1
        if verdict.status == UNKNOWN:
            unknown += 1
            continue
        actual_psi_v, rowchg = ground_truth(tbl, data)
        stale = is_truly_stale(actual_psi_v, rowchg)
        if stale:
            p["true_stale"] += 1
        if verdict.status == "STALE" and stale:
            tp += 1
        elif verdict.status == "STALE" and not stale:
            fp += 1
            p["fp"] += 1
        elif verdict.status == "FRESH" and stale:
            fn += 1
            p["fn"] += 1
        else:
            tn += 1
        cached[-1] = (tbl, verdict.signals, actual_psi_v, rowchg)
    judged = tp + fp + tn + fn
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn, "unknown": unknown,
        "fp_rate": fp / (fp + tn) if fp + tn else 0.0,
        "fn_rate": fn / (tp + fn) if tp + fn else 0.0,
        "recompute": tp + fp + unknown,  # STALE+UNKNOWN 都需重算，FN已漏掉不重算
        "judged": judged,
    }, per, cached


def fmt_pct(x):
    return f"{x * 100:6.2f}%"


def main():
    print("=" * 78)
    print("构建 1000 张表 x 1000 行 = 100 万行模拟数据 ...")
    t0 = time.perf_counter()
    tables = build_tables()
    print(f"数据构建耗时 {time.perf_counter() - t0:.2f}s\n")

    print("=" * 78)
    print("一、默认阈值对照（重算 100 万行全量统计作为真值）")
    print("=" * 78)
    th = Thresholds()
    print(f"阈值: change_ratio>={th.change_ratio}  PSI>={th.psi}  "
          f"max_age>={th.max_age}  min_sample>={th.min_sample}")
    print(f"真值定义: 重算 PSI>={TRUTH_PSI} 或 行数变化>={TRUTH_ROWCHG:.0%}\n")

    stats, per, cached = evaluate(tables, th)

    hdr = f"{'场景':<10}{'表数':>5}{'真实过期':>9}{'FRESH':>7}{'STALE':>7}{'UNKNOWN':>9}{'误判':>5}{'漏判':>5}"
    print(hdr)
    print("-" * len(hdr))
    labels = [("never", "从未变更"), ("micro", "微变"), ("churn", "同分布换血"),
              ("moderate", "中等漂移"), ("drastic", "剧变"), ("missing", "统计缺失")]
    for key, label in labels:
        p = per[key]
        print(f"{label:<10}{p['total']:>5}{p['true_stale']:>9}"
              f"{p['FRESH']:>7}{p['STALE']:>7}{p['UNKNOWN']:>9}"
              f"{p['fp']:>5}{p['fn']:>5}")
    print("-" * len(hdr))
    print(f"{'合计':<10}{1000:>5}{'':>9}{'':>7}{'':>7}{stats['unknown']:>9}"
          f"{stats['fp']:>5}{stats['fn']:>5}")
    print(f"\n混淆矩阵（{stats['judged']} 张可判定表 + {stats['unknown']} 张 UNKNOWN）:")
    print(f"  TP={stats['tp']}  FP(误判)={stats['fp']}  "
          f"TN={stats['tn']}  FN(漏判)={stats['fn']}")
    print(f"  误判率 FPR = FP/(FP+TN) = {fmt_pct(stats['fp_rate'])}")
    print(f"  漏判率 FNR = FN/(TP+FN) = {fmt_pct(stats['fn_rate'])}")
    print(f"  需要重算表数(STALE+UNKNOWN) = {stats['recompute']} / 1000")

    print("\n典型信号样本（每场景抽 1 张，检测信号 vs 重算真值）:")
    print(f"{'场景':<8}{'change_ratio':>14}{'样本PSI':>10}{'年龄':>6}"
          f"{'重算PSI':>10}{'行变率':>8}  判定")
    seen = set()
    for scenario, tbl, data in tables:
        if scenario in seen:
            continue
        seen.add(scenario)
        v = Detector(th).assess(tbl, NOW)
        if v.status == UNKNOWN:
            print(f"{scenario:<8}{'--':>14}{'--':>10}{'--':>6}{'--':>10}{'--':>8}"
                  f"  UNKNOWN（无快照）")
            continue
        ap, rc = ground_truth(tbl, data)
        cr = v.signals.change_ratio
        dp = v.signals.drift_psi
        dp_s = f"{dp:>10.3f}" if dp is not None else f"{'--':>10}"
        print(f"{scenario:<8}{cr:>14.3f}{dp_s}"
              f"{v.signals.age:>6}{ap:>10.3f}{rc:>8.3f}  {v.status}")

    print("\n" + "=" * 78)
    print("二、阈值对比（误判率 / 漏判率 / 重算次数）")
    print("=" * 78)

    INF = 10 ** 9
    rows_sweep = [
        (dict(change_ratio=0.05, psi=0.10, max_age=30), "三路都严，时间主导"),
        (dict(), "默认：change=0.2 PSI=0.25 age=100 veto开"),
        (dict(psi=0.10), "漂移阈值收紧到 0.10"),
        (dict(psi=0.50), "漂移阈值放宽到 0.50"),
        (dict(drift_veto=False), "简单或逻辑（veto关）"),
        (dict(change_ratio=INF, psi=INF, max_age=40, drift_veto=False),
         "只信时间 max_age=40"),
        (dict(change_ratio=INF, psi=INF, max_age=100, drift_veto=False),
         "只信时间 max_age=100"),
        (dict(change_ratio=0.20, psi=INF, max_age=INF, drift_veto=False),
         "只信变更量 change=0.2"),
    ]
    print(f"{'误判率':>9}{'漏判率':>9}{'重算表数':>10}   配置")
    print("-" * 78)
    for kw, note in rows_sweep:
        s, _, _ = evaluate(tables, Thresholds(**kw))
        print(f"{fmt_pct(s['fp_rate']):>9}{fmt_pct(s['fn_rate']):>9}"
              f"{s['recompute']:>8}/1000   {note}")

    print("\n" + "=" * 78)
    print("三、性能：100 万行 / 1000 张表")
    print("=" * 78)
    # 1) 全量 analyze（重算基线，对照成本）
    t0 = time.perf_counter()
    for _, tbl, data in tables:
        Snapshot(tbl.snapshot.taken_at if tbl.snapshot else NOW,
                 len(data), Histogram(data))
    analyze_ms = (time.perf_counter() - t0) * 1000

    # 2) 检测路径（信号计算 + 判定），测时 + tracemalloc
    det = Detector(th)
    t0 = time.perf_counter()
    verdicts = [det.assess(tbl, NOW) for _, tbl, _ in tables]
    detect_ms = (time.perf_counter() - t0) * 1000

    tracemalloc.start()
    _ = [det.assess(tbl, NOW) for _, tbl, _ in tables]
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # 3) 每表常驻元数据体积
    HIST_BYTES = 12 * 8  # 12 个桶（10 个等宽桶 + 下溢/上溢）
    meta_bytes = 0
    for _, tbl, _ in tables:
        meta_bytes += (sys.getsizeof(tbl) + sys.getsizeof(tbl.sample)
                       + len(tbl.sample) * sys.getsizeof(0.0)
                       + HIST_BYTES)
    print(f"全量重算 analyze 100 万行   : {analyze_ms:9.1f} ms")
    print(f"增量检测 1000 张表          : {detect_ms:9.3f} ms "
          f"（均摊 {detect_ms / 1000:.4f} ms/表）")
    print(f"检测过程 Python 堆分配峰值  : {peak / 1024:9.1f} KiB")
    print(f"每表常驻元数据(计数+128蓄水池+直方图): {meta_bytes / 1000:8.1f} B/表"
          f"，1000 表合计 {meta_bytes / 1024:8.1f} KiB")
    stale_n = sum(v.status == "STALE" for v in verdicts)
    unk_n = sum(v.status == "UNKNOWN" for v in verdicts)
    print(f"检测结果: STALE={stale_n} UNKNOWN={unk_n} "
          f"FRESH={1000 - stale_n - unk_n}")

    print("\n" + "=" * 78)
    print("四、硬性断言（任一失败则退出码=1）")
    print("=" * 78)
    checks = [
        ("统计缺失 -> 全部 UNKNOWN", per["missing"]["UNKNOWN"] == 50),
        ("从未变更 -> 全部 FRESH", per["never"]["FRESH"] == 400),
        ("微变 -> 全部 FRESH", per["micro"]["FRESH"] == 200),
        ("同分布换血 -> 全部 FRESH（veto 生效）", per["churn"]["FRESH"] == 150),
        ("剧变 -> 全部 STALE", per["drastic"]["STALE"] == 150),
        ("默认阈值误判数为 0", stats["fp"] == 0),
        ("默认阈值漏判数 = 24（中等漂移边界表）", stats["fn"] == 24),
        ("UNKNOWN 表不进入误判/漏判统计", stats["unknown"] == 50),
    ]
    failed = 0
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        failed += not ok
    if failed:
        print(f"\n{failed} 项断言失败")
        sys.exit(1)
    print("\n全部断言通过")


if __name__ == "__main__":
    main()

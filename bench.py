"""性能对比：100 万行基线 + 10 万次变化，增量刷新 vs 全量重算。

公平性：
- 两种刷新路径面对同一张表、同一份最终数据状态；
- 都从同一 MVCC 快照读数据；
- 变化的「写入/提交」耗时单独计时，不计入任何一方；
- 各跑 3 轮取最佳/均值；增量轮在计时外重建基线视图，只计应用日志的时间。

另含「变化量大到不如全量」场景：10 万行 + 8 万次变化（>60%），
刷新器识别后退回全量，并给出两种路径的实际耗时。

输出：artifacts/bench_report.json
"""

import json
import os
import random
import time

from mvlib import Table, MaterializedView

ARTIFACTS = os.path.join(os.path.dirname(__file__), "artifacts")
REGIONS = ["north", "south", "east", "west", "mid", "isle"]
ROUNDS = 3


def timed(fn, *a, **kw):
    t0 = time.perf_counter()
    ret = fn(*a, **kw)
    return time.perf_counter() - t0, ret


def build_big(n_rows, seed=1):
    rng = random.Random(seed)
    t = Table("orders")

    def gen():
        for i in range(1, n_rows + 1):
            yield {"id": i, "region": REGIONS[rng.randrange(6)],
                   "amount": rng.randint(1, 50)}

    load_t, xid = timed(t.bulk_load, gen())
    return t, rng, load_t


def apply_mixed_changes(t, rng, n_base, n_del, n_upd, n_ins):
    ops = []
    del_ids = set(rng.sample(range(1, n_base + 1), n_del))
    rest = [p for p in range(1, n_base + 1) if p not in del_ids]
    upd_ids = rng.sample(rest, n_upd)
    for pk in del_ids:
        ops.append(("DELETE", pk))
    for pk in upd_ids:
        ops.append(("UPDATE", pk))
    for i in range(n_base + 1, n_base + 1 + n_ins):
        ops.append(("INSERT", i))
    rng.shuffle(ops)
    t0 = time.perf_counter()
    for op, pk in ops:
        if op == "DELETE":
            t.delete(pk)
        elif op == "UPDATE":
            t.update(pk, {"region": REGIONS[rng.randrange(6)],
                          "amount": rng.randint(1, 50)})
        else:
            t.insert({"id": pk, "region": REGIONS[rng.randrange(6)],
                      "amount": rng.randint(1, 50)})
    return time.perf_counter() - t0, len(ops)


def stats(xs):
    return {"best_s": round(min(xs), 4),
            "avg_s": round(sum(xs) / len(xs), 4),
            "all_s": [round(x, 4) for x in xs]}


def main():
    os.makedirs(ARTIFACTS, exist_ok=True)
    report = {"python": "%d.%d.%d" % __import__("sys").version_info[:3],
              "sections": []}

    # ---------- 主场景：100 万行 + 10 万次混合变化 ----------
    n_base, n_del, n_upd, n_ins = 1_000_000, 30_000, 30_000, 40_000
    n_chg = n_del + n_upd + n_ins
    print("装载基线 %d 行 ..." % n_base)
    t, rng, load_t = build_big(n_base)
    mv0 = MaterializedView("mv", t, "region", "amount")
    base_t, _ = timed(mv0.refresh)
    print("基线全量构建 %.3fs；提交 %d 次变化 ..." % (base_t, n_chg))
    snap_pre = t.snapshot()          # 变化前快照：增量轮的基线固定在这里
    ingest_t, n_ops = apply_mixed_changes(t, rng, n_base, n_del, n_upd, n_ins)
    live = t.live_count()

    inc_ts, full_ts = [], []
    inc_rows = full_rows = None
    applied = 0
    for r in range(ROUNDS):
        mv = MaterializedView("mv_inc", t, "region", "amount")
        mv.rebuild(snap_pre)  # 计时外：每轮把基线重建到「变化前」快照
        dt, rep = timed(mv.refresh)
        inc_ts.append(dt)
        if r == 0:
            inc_rows = mv.rows()
            applied = rep.applied
        mv2 = MaterializedView("mv_full", t, "region", "amount")
        mv2.rebuild()  # 保证基线存在
        dt2, _ = timed(mv2.rebuild)
        full_ts.append(dt2)
        if r == 0:
            full_rows = mv2.rows()
    equal = inc_rows == full_rows
    main_section = {
        "name": "主场景：100万行 + 10万次混合变化(删3万/改3万/增4万)",
        "baseline_rows": n_base,
        "changes": n_ops,
        "final_live_rows": live,
        "bulk_load_s": round(load_t, 4),
        "baseline_full_build_s": round(base_t, 4),
        "change_ingest_s(单独计时)": round(ingest_t, 4),
        "incremental_refresh": stats(inc_ts),
        "full_recompute": stats(full_ts),
        "incremental_applied_changes": applied,
        "speedup_best": round(min(full_ts) / min(inc_ts), 1),
        "rows_equal": equal,
        "view_sample_rows": [dict(zip(
            ("region", "cnt", "sum_amount", "min_amount", "max_amount"), r))
            for r in full_rows],
    }
    report["sections"].append(main_section)
    print("增量 %ss  全量 %ss  加速比 %sx  逐行一致=%s"
          % (stats(inc_ts)["best_s"], stats(full_ts)["best_s"],
             main_section["speedup_best"], equal))

    # ---------- 成本回退场景：变化量 > 存活行数 × 0.6 ----------
    n2, d2, u2, i2 = 100_000, 40_000, 0, 40_000
    print("\n成本回退场景：%d 行 + %d 次变化 ..." % (n2, d2 + u2 + i2))
    t2, rng2, load2 = build_big(n2, seed=2)
    mvx = MaterializedView("mvx", t2, "region", "amount")
    mvx.refresh()
    ingest2, _ = apply_mixed_changes(t2, rng2, n2, d2, u2, i2)
    dt_fb, rep_fb = timed(mvx.refresh)
    mvx2 = MaterializedView("mvx2", t2, "region", "amount")
    mvx2.rebuild()
    dt_full, _ = timed(mvx2.rebuild)
    fallback_section = {
        "name": "变化量大场景：10万行 + 8万次变化(删4万/增4万)",
        "baseline_rows": n2,
        "changes": d2 + u2 + i2,
        "final_live_rows": t2.live_count(),
        "chosen_mode": rep_fb.mode,
        "fallback_reason": rep_fb.reason,
        "refresh_latency_s": round(dt_fb, 4),
        "if_naive_incremental_full_scan_s": round(dt_full, 4),
        "note": "增量不划算时自动退回全量；延迟即一次全量扫描成本",
    }
    report["sections"].append(fallback_section)
    print("选择模式=%s  刷新耗时 %.3fs  原因=%s"
          % (rep_fb.mode, dt_fb, rep_fb.reason))

    out = os.path.join(ARTIFACTS, "bench_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n报告: %s" % out)
    return 0 if equal else 1


if __name__ == "__main__":
    raise SystemExit(main())

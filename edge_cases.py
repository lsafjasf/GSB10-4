"""边界用例自测（断言 + 说明）：

1. 覆盖索引足够：投影被索引覆盖 -> index_only，且 0 次随机回表页读。
2. 必须回表：投影含 payload + 极小选择率 -> lookup，结果与全扫一致。
3. 极端选择率：空范围（0 行）、100% 范围（覆盖时索引扫描 / 非覆盖时顺序全扫，
   绝不回表扫 100 万次随机页）。
4. 统计信息缺失：估算显式失败（feasible=False + 原因），走保守计划。
另含三种计划结果一致性的正确性检查。
"""
from cost_model import CostModel, Query
from engine import Executor, Table


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        raise AssertionError(name)


def main():
    t = Table(1_000_000, 50_000)
    stats = t.analyze()
    m = CostModel(t.n)
    ex = Executor(t)

    # 1. 覆盖索引足够
    q = Query("e1", "range", 1000, 1010, True, None, True, "")
    best, alts = m.choose(q, stats)
    r = ex.execute(best.plan, q)
    check("覆盖索引足够 -> index_only", best.plan == "index_only", f"plan={best.plan}")
    check("覆盖索引扫描 0 次随机回表", r.rand_pages == 0, f"rand={r.rand_pages}")
    check("估算页数为顺序页", best.units.get("rand_page", 0.0) == 0.0)

    # 2. 必须回表（投影含 payload，选择率 0.01%）
    q2 = Query("e2", "range", 1000, 1005, False, None, True, "")
    best2, _ = m.choose(q2, stats)
    r2 = ex.execute(best2.plan, q2)
    check("必须回表 -> lookup", best2.plan == "lookup", f"plan={best2.plan}")
    check("回表有随机页读", r2.rand_pages > 0)
    r2f = ex.execute("full_scan", q2)
    check("回表结果与全扫一致", r2.rows == r2f.rows == t.actual_count(q2),
          f"rows={r2.rows}")
    check("回表页数 ≈ distinct_pages 公式", abs(r2.rand_pages - best2.units["rand_page"])
          / max(r2.rand_pages, 1) < 0.25,
          f"actual={r2.rand_pages} est={best2.units['rand_page']:.0f}")

    # 3. 极端选择率
    qempty = Query("e3a", "range", 10**9, 10**9 + 1, True, None, True, "")
    be, _ = m.choose(qempty, stats)
    re = ex.execute(be.plan, qempty)
    check("空范围 -> 0 行", re.rows == 0 and be.est_rows == 0.0)
    check("空范围代价仅为固定开销级", be.units["cpu_row"] == 0.0)

    qall = Query("e3b", "range", 0, 50_000, False, None, True, "")
    ball, alts = m.choose(qall, stats)
    check("100% 非覆盖 -> full_scan（拒绝全量随机回表）", ball.plan == "full_scan",
          f"plan={ball.plan}")
    # 定量说明：此时回表估算触及的堆页数应接近全部堆页且为随机读
    lk = m.estimate_plan("lookup", qall, stats)
    check("100% 时回表估算堆页≈全部堆页", lk.units["rand_page"] > 0.9 * t.heap_pages,
          f"rand_pages_est={lk.units['rand_page']:.0f}/{t.heap_pages}")

    # 选择率翻转点附近（非阈值：纯粹由代价最小化决定）
    flipped = None
    for w in range(1, 50_001, 100):
        qw = Query("e3c", "range", 0, w, False, None, True, "")
        bw, aw = m.choose(qw, stats)
        if bw.plan != "lookup":
            flipped = (w, bw.plan, [a.total_ms for a in aw])
            break
    check("选择率升高时计划由 lookup 翻转为 full_scan", flipped is not None,
          f"width={flipped[0] if flipped else None}")

    # 4. 统计信息缺失
    qg = Query("e4", "range", 1000, 1010, False, None, False, "")
    bg, _ = m.choose(qg, None)
    check("统计缺失 -> feasible=False", not bg.feasible)
    check("统计缺失给出显式原因", len(bg.reason) > 10)
    check("统计缺失 -> 保守 full_scan（不猜回表）", bg.plan == "full_scan", bg.reason)
    qgc = Query("e4b", "range", 1000, 1010, True, None, False, "")
    bgc, _ = m.choose(qgc, None)
    check("统计缺失+覆盖 -> 保守 index_only", bgc.plan == "index_only" and not bgc.feasible)

    # 5. 三种计划结果一致性抽查
    for lo, hi in [(500, 502), (20000, 20050), (0, 50_000)]:
        q = Query("x", "range", lo, hi, True, None, True, "")
        counts = {p: ex.execute(p, q).rows for p in ("index_only", "lookup", "full_scan")}
        check(f"计划结果一致 [{lo},{hi}]",
              counts["index_only"] == counts["lookup"] == counts["full_scan"],
              str(counts))

    print("\n全部边界用例通过。")


if __name__ == "__main__":
    main()

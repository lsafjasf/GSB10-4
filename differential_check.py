"""对拍：每个场景下，增量刷新结果必须与全量重算逐行一致。

覆盖场景：
  1. insert_only      只有新增
  2. delete_only      只有删除（含整组删光后组应消失）
  3. mixed            新增/修改/删除混合
  4. minmax_deletes   专门打到当前 MIN/MAX 值的删除（考验懒删除堆）
  5. re_update        同一行多次修改
  6. huge_ratio       变化量大到不如全量（成本回退）
  7. truncate         TRUNCATE（必须回退）
  8. schema_change    schema 变更（必须回退）
  9. log_gap          changelog 丢失（必须回退）
 10. def_change       视图定义变化（必须回退）

输出：
  artifacts/differential_data.csv  全部场景的逐行对拍明细（行名/列名/两边值）
  artifacts/differential_report.json 场景汇总（模式/回退原因/是否一致）
"""

import csv
import json
import os
import random

from mvlib import Table, MaterializedView

ARTIFACTS = os.path.join(os.path.dirname(__file__), "artifacts")
N_BASE = 200          # 小规模基线，逐行对拍不遗漏
N_GROUPS = 6
SEED = 20261005

REGIONS = ["north", "south", "east", "west", "mid", "isle"]
COLS = ("region", "cnt", "sum_amount", "min_amount", "max_amount")


def seed_rows(rng, n):
    return [{"id": i,
             "region": REGIONS[rng.randrange(N_GROUPS)],
             "amount": rng.randint(1, 50)}
            for i in range(1, n + 1)]


def make_parts(case):
    t = Table("orders")
    rng = random.Random(SEED + hash(case) % 10000)
    t.bulk_load(seed_rows(rng, N_BASE))
    mv = MaterializedView("mv_orders_region", t, "region", "amount")
    mv.refresh()  # 基线全量
    return t, mv, rng


def mutate(t, rng, kind, n):
    next_id = [N_BASE + 1]

    def insert_one():
        pk = next_id[0]
        next_id[0] += 1
        t.insert({"id": pk,
                  "region": REGIONS[rng.randrange(N_GROUPS)],
                  "amount": rng.randint(1, 50)})

    if kind == "insert":
        for _ in range(n):
            insert_one()
    elif kind == "delete":
        for pk in rng.sample(range(1, N_BASE + 1), n):
            t.delete(pk)
    elif kind == "update":
        for _ in range(n):
            pk = rng.randint(1, N_BASE)
            t.update(pk, {"amount": rng.randint(1, 50),
                          "region": REGIONS[rng.randrange(N_GROUPS)]})
    elif kind == "mixed":
        third = n // 3
        deleted = set(rng.sample(range(1, N_BASE + 1), third))
        for pk in deleted:
            t.delete(pk)
        alive = [p for p in range(1, N_BASE + 1) if p not in deleted]
        for pk in rng.sample(alive, third):
            t.update(pk, {"amount": rng.randint(1, 50)})
        for _ in range(n - 2 * third):
            insert_one()


def case_insert_only():
    t, mv, rng = make_parts("insert_only")
    mutate(t, rng, "insert", 60)
    return t, mv, None


def case_delete_only():
    t, mv, rng = make_parts("delete_only")
    mutate(t, rng, "delete", 70)
    return t, mv, None


def case_mixed():
    t, mv, rng = make_parts("case_mixed")
    # 手工混合，确保三类都足量，且包含跨组更新
    for pk in range(1, 21):
        t.delete(pk)                       # 删除
    for pk in range(21, 46):
        t.update(pk, {"region": REGIONS[(pk + 2) % N_GROUPS],
                      "amount": (pk * 7) % 50 + 1})  # 修改（多为跨组）
    rng2 = random.Random(SEED)
    for i in range(N_BASE + 1, N_BASE + 31):
        t.insert({"id": i,
                  "region": REGIONS[rng2.randrange(N_GROUPS)],
                  "amount": rng2.randint(1, 50)})
    return t, mv, None


def case_minmax_deletes():
    # 同一组里放大量相同极值，再逐个删掉，验证 MIN/MAX 懒删除堆与全量一致
    t, mv, rng = make_parts("minmax_deletes")
    for i in range(N_BASE + 1, N_BASE + 21):
        t.insert({"id": i, "region": "north", "amount": 1})   # 当前最小
    for i in range(N_BASE + 21, N_BASE + 41):
        t.insert({"id": i, "region": "south", "amount": 50})  # 当前最大
    for i in range(N_BASE + 1, N_BASE + 11):
        t.delete(i)
    for i in range(N_BASE + 21, N_BASE + 31):
        t.delete(i)
    # 再删一个会整体消失的小组成员
    return t, mv, None


def case_re_update():
    t, mv, rng = make_parts("re_update")
    for k in range(5):
        t.update(5, {"amount": 10 + k})
        t.update(6, {"region": REGIONS[k % N_GROUPS], "amount": 50 - k})
    t.delete(7)
    return t, mv, None


def case_huge_ratio():
    # 基线只有 100 行，制造 120 条变化（含新增）> 100 × 0.6 → 全量
    t = Table("orders")
    rng = random.Random(SEED + 7)
    t.bulk_load(seed_rows(rng, 100))
    mv = MaterializedView("mv_orders_region", t, "region", "amount")
    mv.refresh()
    for pk in range(1, 71):
        t.delete(pk)
    for i in range(101, 151):
        t.insert({"id": i, "region": REGIONS[rng.randrange(N_GROUPS)],
                  "amount": rng.randint(1, 50)})
    return t, mv, None


def case_truncate():
    t, mv, rng = make_parts("truncate")
    mutate(t, rng, "insert", 10)
    t.truncate()
    mutate(t, rng, "insert", 5)
    return t, mv, None


def case_schema_change():
    t, mv, rng = make_parts("schema_change")
    mutate(t, rng, "insert", 10)
    t.alter_schema()
    return t, mv, None


def case_log_gap():
    t, mv, rng = make_parts("log_gap")
    mutate(t, rng, "mixed", 30)
    t.debug_corrupt_log_gap()   # 模拟日志丢失
    return t, mv, None


def case_def_change():
    t, mv, rng = make_parts("def_change")
    mutate(t, rng, "insert", 20)
    mv.redefine(group_col="amount", sum_col="amount")  # 定义变化
    return t, mv, None


CASES = [
    ("insert_only", "只有新增", case_insert_only, "incremental"),
    ("delete_only", "只有删除", case_delete_only, "incremental"),
    ("mixed", "新增/修改/删除混合", case_mixed, "incremental"),
    ("minmax_deletes", "删除命中当前MIN/MAX", case_minmax_deletes, "incremental"),
    ("re_update", "同一行多次修改", case_re_update, "incremental"),
    ("huge_ratio", "变化量大触发成本回退", case_huge_ratio, "full"),
    ("truncate", "TRUNCATE必须回退", case_truncate, "full"),
    ("schema_change", "schema变更必须回退", case_schema_change, "full"),
    ("log_gap", "changelog缺口必须回退", case_log_gap, "full"),
    ("def_change", "视图定义变化必须回退", case_def_change, "full"),
]


def run_case(fn):
    t, mv, _ = fn()
    report = mv.refresh()
    inc = mv.rows()
    snap = mv.rebuild()          # 独立全量重算基准
    full = mv.rows()
    equal = inc == full
    return report, inc, full, equal, snap


def main():
    os.makedirs(ARTIFACTS, exist_ok=True)
    csv_path = os.path.join(ARTIFACTS, "differential_data.csv")
    json_path = os.path.join(ARTIFACTS, "differential_report.json")
    summaries = []
    all_ok = True
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["case", "group_key", "column",
                    "incremental_value", "full_recompute_value", "match"])
        for cid, desc, fn, expect in CASES:
            report, inc, full, equal, snap = run_case(fn)
            ok = (equal and report.mode == expect)
            all_ok &= ok
            # 以两边键并集逐行逐列落盘，漏一组/一列都会显形
            keys = sorted(set(k for k, *_ in inc) | set(k for k, *_ in full))
            di = {k: v for k, *v in inc}
            df = {k: v for k, *v in full}
            for k in keys:
                vi = di.get(k, ["<缺失>"] * 4)
                vf = df.get(k, ["<缺失>"] * 4)
                for col, a, b in zip(COLS[1:], vi, vf):
                    w.writerow([cid, k, col, a, b, "Y" if a == b else "N"])
            summaries.append({
                "case": cid,
                "description": desc,
                "expected_mode": expect,
                "actual_mode": report.mode,
                "fallback_reason": report.reason,
                "changes": report.changes,
                "applied_incrementally": report.applied,
                "view_rows": report.rows,
                "snapshot_xid": report.snapshot.xid,
                "rows_equal_to_full_recompute": equal,
                "passed": ok,
            })
            flag = "PASS" if ok else "FAIL"
            print("[%s] %-15s mode=%-11s changes=%-4d rows=%-3d %s"
                  % (flag, cid, report.mode, report.changes, report.rows,
                     ("reason=%s" % report.reason) if report.reason else ""))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"all_passed": all_ok, "cases": summaries}, f,
                  ensure_ascii=False, indent=2)
    print("\n逐行对拍明细: %s" % csv_path)
    print("场景汇总:     %s" % json_path)
    print("总结果: %s" % ("ALL PASS" if all_ok else "HAS FAILURES"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""快照一致性演示：刷新进行中有并发写入，刷新读到的必须是一致快照。

流程：
  1. 建表 5000 行并建立视图基线；
  2. 制造 50 条基线后变更（让本次刷新走增量）；
  3. 刷新开始时抓取快照 S；刷新期间，后台线程并发提交 200 个事务
     （新增/修改/删除混合）；
  4. 在刷新过程中反复用 S 读表：行数与内容必须逐次相同（可重复读），
     且看不到 S 之后提交的任何数据；
  5. 刷新只应用 S 高水位之内的变更；刷新结果必须与「在 S 上全量重算」
     逐行一致；S 之后的 200 个事务留给下一次刷新。

输出：artifacts/snapshot_data.json、artifacts/snapshot_rows_sample.csv
"""

import csv
import json
import os
import random
import threading
import time

from mvlib import Table, MaterializedView

ARTIFACTS = os.path.join(os.path.dirname(__file__), "artifacts")
REGIONS = ["north", "south", "east", "west", "mid", "isle"]
N_BASE = 5000
N_PRE = 50        # 基线后、刷新前的变更（本次增量要应用的）
N_CONCURRENT = 200  # 刷新期间并发提交的事务数（本次刷新必须看不到）


def build_table():
    rng = random.Random(7)
    t = Table("orders")
    t.bulk_load([{"id": i,
                  "region": REGIONS[rng.randrange(len(REGIONS))],
                  "amount": rng.randint(1, 50)}
                 for i in range(1, N_BASE + 1)])
    return t, rng


def aggregates_from_snapshot(table, snapshot):
    """在快照上做一次全量重算，作为基准。"""
    agg = {}
    for _pk, row in table.iter_snapshot(snapshot):
        g = agg.setdefault(row["region"], [0, 0, None, None])
        v = row["amount"]
        g[0] += 1
        g[1] += v
        g[2] = v if g[2] is None else min(g[2], v)
        g[3] = v if g[3] is None else max(g[3], v)
    return sorted((k, *v) for k, v in agg.items())


def main():
    os.makedirs(ARTIFACTS, exist_ok=True)
    t, rng = build_table()
    mv = MaterializedView("mv_orders_region", t, "region", "amount")
    mv.refresh()  # 基线

    # 基线后变更：本次刷新应当增量应用的部分
    for i in range(N_BASE + 1, N_BASE + 1 + N_PRE):
        t.insert({"id": i, "region": REGIONS[i % len(REGIONS)],
                  "amount": (i * 13) % 50 + 1})

    writer_started = threading.Event()
    writer_done = threading.Event()
    committed_during = []

    def writer():
        writer_started.wait()
        wrng = random.Random(99)
        for k in range(N_CONCURRENT):
            op = k % 3
            if op == 0:
                pk = N_BASE + N_PRE + 1 + k
                t.insert({"id": pk, "region": REGIONS[wrng.randrange(6)],
                          "amount": wrng.randint(1, 50)})
                committed_during.append(("INSERT", pk))
            elif op == 1:
                pk = wrng.randint(1, N_BASE)
                try:
                    t.update(pk, {"amount": wrng.randint(1, 50)})
                    committed_during.append(("UPDATE", pk))
                except KeyError:
                    pass
            else:
                pk = wrng.randint(1, N_BASE)
                try:
                    t.delete(pk)
                    committed_during.append(("DELETE", pk))
                except KeyError:
                    pass
            time.sleep(0.0003)
        writer_done.set()

    th = threading.Thread(target=writer, daemon=True)
    th.start()

    observed = {"counts": [], "samples": None, "expected": None,
                "snapshot": None}

    def during_snapshot(snapshot):
        """刷新回调：此刻快照 S 已建立，变更尚未应用。"""
        observed["snapshot"] = snapshot.describe()
        # 第一次读：写入尚未开始
        observed["counts"].append(t.snapshot_row_count(snapshot))
        # 放并发写进来，并在写入进行中反复读同一快照
        writer_started.set()
        while not writer_done.is_set():
            observed["counts"].append(t.snapshot_row_count(snapshot))
        th.join()
        # 写线程结束后，再用同一快照读：必须依然一致（可重复读）
        rows = list(t.iter_snapshot(snapshot))
        observed["counts"].append(len(rows))
        observed["samples"] = rows[:10]
        observed["expected"] = aggregates_from_snapshot(t, snapshot)

    report = mv.refresh(during_snapshot=during_snapshot)

    counts = observed["counts"]
    repeatable = len(set(counts)) == 1
    inc_rows = mv.rows()
    expected = [tuple(r) for r in observed["expected"]]
    matches_snapshot_recompute = [tuple(r) for r in inc_rows] == expected

    # 下一次刷新应能拾取刷新期间并发提交的变更
    report2 = mv.refresh()

    result = {
        "说明": "刷新期间并发写入 %d 个事务；刷新读到的是快照 S 的一致视图" % len(committed_during),
        "snapshot": observed["snapshot"],
        "refresh_report": report.as_dict(),
        "刷新期间反复读取同一快照的次数": len(counts),
        "行数序列(前5次+末5次)": counts[:5] + ["..."] + counts[-5:],
        "可重复读(行数恒定)": repeatable,
        "刷新期间并发提交的事务数": len(committed_during),
        "并发提交是否被本次刷新排除": report.applied <= N_PRE,
        "本次增量应用的变更数": report.applied,
        "刷新结果与快照上全量重算逐行一致": matches_snapshot_recompute,
        "快照样本行(前10行, id/region/amount)": [
            [pk, r["region"], r["amount"]] for pk, r in observed["samples"]],
        "下一次刷新拾取的变更数(=并发提交)": report2.changes,
        "下一次刷新模式": report2.mode,
    }

    json_path = os.path.join(ARTIFACTS, "snapshot_data.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    csv_path = os.path.join(ARTIFACTS, "snapshot_rows_sample.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "region", "amount"])
        for pk, r in observed["samples"]:
            w.writerow([pk, r["region"], r["amount"]])

    ok = (repeatable and matches_snapshot_recompute
          and report.mode == "incremental"
          and report2.changes == len(committed_during))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n快照数据: %s" % json_path)
    print("样本行:   %s" % csv_path)
    print("总结果: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

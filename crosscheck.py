#!/usr/bin/env python3
"""对拍：同一批条目，分别走「批量提交」与「逐项单独提交」两条路径，
比对逐项状态表与服务端副作用流水，必须完全一致。

用法：python3 crosscheck.py [条目数] [seed]
"""
from __future__ import annotations

import sys

from idempotent_batch import FakeServer, Workload, Item, submit_batch, submit_single


def build(n, seed):
    server = FakeServer()
    wl = Workload.seeded_mix(server, n, seed=seed)
    items = [Item(id=k, payload={"v": k}, idem_key=k) for k, _, _ in wl.specs]
    return server, wl, items


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 275

    # 路径 A：批量（并发）
    server_a, wl_a, items_a = build(n, seed)
    results_a, stats_a = submit_batch(server_a, items_a, workers=32)

    # 路径 B：逐项单独提交（串行，独立服务端实例）
    server_b, wl_b, items_b = build(n, seed)
    results_b = [submit_single(server_b, it) for it in items_b]

    assert [k for k, _, _ in wl_a.specs] == [k for k, _, _ in wl_b.specs]

    mismatches = []
    for ra, rb in zip(results_a, results_b):
        if ra.status != rb.status or ra.attempts != rb.attempts:
            mismatches.append((ra.id, ra.status.value, rb.status.value,
                               ra.attempts, rb.attempts))

    eff_a, eff_b = server_a.effects_snapshot(), server_b.effects_snapshot()
    effects_equal = eff_a == eff_b
    dup_a = len(eff_a) != len(set(eff_a))
    dup_b = len(eff_b) != len(set(eff_b))

    print(f"=== 对拍数据（n={n}, seed={seed}）===")
    print(f"批量路径   : 状态分布 {stats_a.by_status}")
    print(f"             提交调用 {stats_a.submits}, 重试 {stats_a.retries}, "
          f"对账 {stats_a.reconciles}, 持久层核实 {stats_a.durable_lookups}, "
          f"耗时 {stats_a.wall_seconds:.3f}s")
    print(f"逐项路径   : 状态分布 {_dist(results_b)}")
    print(f"             提交调用 {sum(r.attempts for r in results_b)}, "
          f"重试 {sum(r.retries for r in results_b)}, "
          f"对账 {sum(r.reconciles for r in results_b)}, "
          f"持久层核实 {sum(r.durable_lookups for r in results_b)}")
    print(f"逐项状态表一致 : {not mismatches}  (不一致 {len(mismatches)} 条)")
    print(f"副作用流水一致 : {effects_equal}  (A={len(eff_a)} 条, B={len(eff_b)} 条)")
    print(f"副作用无重复   : A={not dup_a}, B={not dup_b}")

    # 抽样打印对拍明细（前 12 条 + 每个非常规状态各 1 条）
    print("\n抽样对拍明细（item_id | 批量状态 | 逐项状态 | 提交次数 A/B）:")
    shown, seen_status = 0, set()
    for ra, rb in zip(results_a, results_b):
        interesting = ra.status.value not in ("SUCCESS",) or shown < 8
        if interesting and (shown < 8 or ra.status.value not in seen_status):
            seen_status.add(ra.status.value)
            print(f"  {ra.id}  {ra.status.value:<26} {rb.status.value:<26} "
                  f"{ra.attempts}/{rb.attempts}")
            shown += 1
        if shown >= 14:
            break

    if mismatches or not effects_equal or dup_a or dup_b:
        for m in mismatches[:10]:
            print("  MISMATCH:", m)
        sys.exit(1)
    print("\n对拍通过：批量与逐项提交结果完全一致。")


def _dist(results):
    d = {}
    for r in results:
        d[r.status.value] = d.get(r.status.value, 0) + 1
    return d


if __name__ == "__main__":
    main()

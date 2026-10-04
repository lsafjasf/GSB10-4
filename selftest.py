#!/usr/bin/env python3
"""自测：全部成功 / 全部失败 / 部分失败 / 重试中再次失败。

用法：
    python3 selftest.py              # 打印逐项状态表并校验
    python3 selftest.py --quiet      # 只打印汇总
"""
from __future__ import annotations

import sys
from collections import Counter

from idempotent_batch import FakeServer, Workload, Item, submit_batch
from idempotent_batch.errors import CATALOG, catalog_rows
from idempotent_batch.client import ItemStatus

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  [FAIL] {msg}")
    return cond


def make_items(wl):
    return [Item(id=k, payload={"v": k}, idem_key=k) for k, _, _ in wl.specs]


def build_scenario(kinds):
    server = FakeServer()
    wl = Workload(server)
    for i, kind in enumerate(kinds):
        wl.add(f"{kind}-{i:03d}", kind)
    return server, wl


def print_table(title, wl, results):
    print(f"\n=== {title} ==="
          f"  （逐项状态表，{len(results)} 条）")
    print(f"{'item_id':<28}{'kind':<22}{'status':<28}"
          f"{'submits':>7}{'retries':>8}{'recon':>6}{'durable':>8}  errors")
    for (key, kind, expected), r in zip(wl.specs, results):
        mark = "" if r.status.value == expected else f"  != 预期 {expected}"
        errs = ",".join(str(e) for e in r.error_history) or "-"
        print(f"{r.id:<28}{kind:<22}{r.status.value:<28}"
              f"{r.attempts:>7}{r.retries:>8}{r.reconciles:>6}{r.durable_lookups:>8}"
              f"  {errs}{mark}")
        check(r.status.value == expected,
              f"{r.id} 状态 {r.status.value} != 预期 {expected}")
        check(r.status in (ItemStatus.SUCCESS, ItemStatus.SUCCESS_AFTER_RECONCILE,
                           ItemStatus.FAILURE_PERMANENT,
                           ItemStatus.FAILURE_RETRY_EXHAUSTED),
              f"{r.id} 出现未知终态 {r.status}")


def verify_side_effects(name, server, results):
    """成功项副作用恰好一次；失败项零副作用；与服务端流水一致。"""
    ok_ids = {r.id for r in results
              if r.status in (ItemStatus.SUCCESS, ItemStatus.SUCCESS_AFTER_RECONCILE)}
    effects = server.effects_snapshot()
    check(len(effects) == len(ok_ids),
          f"[{name}] 副作用数 {len(effects)} != 成功条目数 {len(ok_ids)}")
    check(set(effects) == ok_ids,
          f"[{name}] 副作用集合与成功项不一致")
    dup = [k for k, c in Counter(effects).items() if c > 1]
    check(not dup, f"[{name}] 以下条目副作用重复: {dup}")


def run_scenario(name, kinds):
    server, wl = build_scenario(kinds)
    items = make_items(wl)
    results, stats = submit_batch(server, items, workers=8)
    print_table(name, wl, results)
    verify_side_effects(name, server, results)
    print(f"  -> 状态分布: {dict(stats.by_status)}  "
          f"提交调用 {stats.submits} 次, 对账 {stats.reconciles} 次, "
          f"持久层核实 {stats.durable_lookups} 次, 耗时 {stats.wall_seconds*1000:.1f} ms")
    return results, stats, server


def test_idempotency_direct():
    """直接验证：同一个幂等键在结果未知后重试，服务端只产生一次副作用。"""
    print("\n=== 幂等性专项：超时后服务端其实已提交，重试不得重复副作用 ===")
    server = FakeServer()
    wl = Workload(server)
    wl.add("only-001", "post_504_committed")
    wl.add("only-002", "durable_reconciled")
    items = make_items(wl)
    results, stats = submit_batch(server, items, workers=1)
    print_table("幂等性专项", wl, results)
    verify_side_effects("idempotency", server, results)
    srv = server.stats_snapshot()
    check(srv["effects"] == 2, f"副作用应为 2，实际 {srv['effects']}")
    print(f"  -> 服务端流水: {srv}")


def print_catalog():
    print("=== 错误分类表（分类依据 + 样例）===")
    for code, name, kind, why in catalog_rows():
        print(f"  {code:>4} {name:<16} {kind:<14} {why}")
    unknown = 999
    from idempotent_batch import classify
    check(classify(unknown).value == "AMBIGUOUS",
          "未知错误码必须保守归类为 AMBIGUOUS")
    print("  其他未知错误码（如 999）=> AMBIGUOUS（保守处理，强制对账）")


def main():
    quiet = "--quiet" in sys.argv

    if not quiet:
        print_catalog()

    # S1 全部成功
    run_scenario("S1 全部成功",
                 ["success"] * 3 + ["transient_then_ok", "rate_then_ok"])

    # S2 全部失败（永久失败 + 重试耗尽混合，均为确定失败）
    run_scenario("S2 全部失败",
                 ["permanent_422", "permanent_403",
                  "exhausted_503", "exhausted_timeout"])

    # S3 部分失败
    run_scenario("S3 部分失败",
                 ["success", "transient_then_ok", "permanent_422",
                  "exhausted_503", "post_504_committed", "success",
                  "transient_then_perm", "rate_then_ok"])

    # S4 重试中再次失败：连续超时、对账接口也先故障、超时后实际已提交等
    run_scenario("S4 重试中再次失败",
                 ["timeout_twice_ok", "post_504_committed",
                  "post_500_committed", "durable_reconciled",
                  "exhausted_timeout", "transient_then_perm",
                  "transient_then_ok", "success"])

    test_idempotency_direct()

    print("\n=== 结论 ===")
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项:")
        for msg in FAILURES:
            print(f"  - {msg}")
        sys.exit(1)
    print("全部断言通过：无 UNKNOWN 条目；成功项副作用恰好一次；"
          "重试只作用于失败项。")


if __name__ == "__main__":
    main()

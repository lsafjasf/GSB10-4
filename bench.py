#!/usr/bin/env python3
"""基准：10 万条目混合负载，输出总耗时与重试/对账次数分布。

用法：python3 bench.py [条目数] [并发数]
"""
from __future__ import annotations

import sys

from idempotent_batch import FakeServer, Workload, Item, submit_batch


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 64

    server = FakeServer()
    wl = Workload.seeded_mix(server, n, seed=275)
    items = [Item(id=k, payload={"v": k}, idem_key=k) for k, _, _ in wl.specs]

    results, stats = submit_batch(server, items, workers=workers,
                                  base_delay=0.0002, max_delay=0.02)

    # 校验：无未知状态；成功项副作用恰好一次
    terminal = {"SUCCESS", "SUCCESS_AFTER_RECONCILE",
                "FAILURE_PERMANENT", "FAILURE_RETRY_EXHAUSTED"}
    unknown = [r.id for r in results if r.status.value not in terminal]
    ok_ids = {r.id for r in results if r.status.value.startswith("SUCCESS")}
    effects = server.effects_snapshot()
    effects_ok = (len(effects) == len(ok_ids) and set(effects) == ok_ids)

    print(f"=== 基准：{n} 条目, workers={workers} ===")
    print(f"总耗时        : {stats.wall_seconds:.3f} s")
    print(f"吞吐          : {n / stats.wall_seconds:,.0f} 条/秒")
    print(f"状态分布      : {stats.by_status}")
    print(f"提交调用总数  : {stats.submits}  (首次 {n} + 重试 {stats.submits - n})")
    print(f"重试次数分布  : " + _hist(stats.retry_histogram, "提交次数"))
    print(f"对账次数分布  : " + _hist(stats.reconcile_histogram, "对账次数"))
    print(f"在线对账总数  : {stats.reconciles}")
    print(f"持久层核实    : {stats.durable_lookups}")
    print(f"服务端提交调用: {server.stats_snapshot()['submit_calls']}")
    print(f"副作用总数    : {len(effects)} (成功项 {len(ok_ids)})")
    print(f"校验          : 无未知状态={not unknown}, 副作用与成功项一一对应={effects_ok}")

    if unknown or not effects_ok:
        sys.exit(1)


def _hist(hist, label):
    parts = []
    for k in sorted(hist):
        if k == 0 and label == "对账次数":
            continue
        parts.append(f"{k}次:{hist[k]}")
    return "  ".join(parts) if parts else "(无)"


if __name__ == "__main__":
    main()

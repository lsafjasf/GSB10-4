"""bench.py -- 5 万条规则、10 万次查询的耗时与内存基准。

运行：python3 bench/bench.py
输出：reports/benchmark_report.txt（人读）与 reports/benchmark_data.json（机读）。

基准内容：
* 构建 50,000 条规则（字面 / 段内 glob / 转义字面混合），记录构建耗时与
  tracemalloc 构建期峰值分配；
* 索引常驻内存 router.index_memory_bytes()；
* 100,000 次查询（60% 命中 / 20% 命中更长路径 / 20% 未命中）的总耗时、
  均值与 p50/p95/p99/max；
* 乱序加载对拍：同一批规则两种顺序各建索引，抽样 3,000 条查询比对完整
  命中序列，必须完全一致。
"""

from __future__ import annotations

import json
import os
import platform
import random
import sys
import time
import tracemalloc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from router import Rule, Router  # noqa: E402

N_RULES = 50_000
N_LITERAL = 40_000
N_GLOB = 8_000
N_ESCAPED = 2_000
N_QUERIES = 100_000
SEED = 20261005


def gen_rules(seed: int = SEED):
    rng = random.Random(seed)
    rules = []
    for i in range(N_LITERAL):
        rules.append(Rule("/svc%d/res%d/op%d" % (i % 700, i % 1500, i % 400),
                          priority=rng.choice([0, 1, 2, 5, 10])))
    for i in range(N_GLOB):
        rules.append(Rule("/svc%d/v*/op%d" % (i % 700, i),
                          priority=rng.choice([0, 1, 2, 5, 10])))
    for i in range(N_ESCAPED):
        rules.append(Rule("/svc%d/a\\*b%d/op" % (i % 700, i),
                          priority=rng.choice([0, 1, 2, 5, 10])))
    assert len(rules) == N_RULES
    return rules


def gen_queries(rules, seed: int = SEED + 1):
    rng = random.Random(seed)
    queries = []
    for _ in range(N_QUERIES):
        roll = rng.random()
        if roll < 0.60:  # 命中：取某条规则并实体化
            rule = rng.choice(rules)
            path = rule.pattern.replace("v*", "v%d" % rng.randint(0, 999))
            path = path.replace("\\*", "*")  # 转义星号 -> 请求里的字面 '*'
        elif roll < 0.80:  # 命中且更深：前缀语义下仍命中
            rule = rng.choice(rules)
            path = rule.pattern.replace("v*", "v%d" % rng.randint(0, 999))
            path = path.replace("\\*", "*") + "/extra/deep"
        else:  # 未命中：不存在的服务名
            path = "/svc%d/res%d/op%d" % (rng.randint(700, 1400),
                                          rng.randint(0, 1500),
                                          rng.randint(0, 400))
        queries.append(path)
    return queries


def percentile(sorted_vals, pct):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def fmt_bytes(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%.2f %s" % (n, unit)
        n /= 1024.0
    return "%d B" % n


def main():
    rules = gen_rules()
    queries = gen_queries(rules)

    # -- 构建（含 tracemalloc 峰值） --
    tracemalloc.start()
    t0 = time.perf_counter()
    router = Router(rules)
    build_s = time.perf_counter() - t0
    _, build_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    index_bytes = router.index_memory_bytes()

    # -- 查询计时 --
    latencies_ns = []
    n_match = n_conflict = n_no_match = 0
    t_start = time.perf_counter()
    for path in queries:
        q0 = time.perf_counter_ns()
        result = router.evaluate(path)
        latencies_ns.append(time.perf_counter_ns() - q0)
        if result.status == "match":
            n_match += 1
        elif result.status == "conflict":
            n_conflict += 1
        else:
            n_no_match += 1
    total_query_s = time.perf_counter() - t_start
    latencies_ns.sort()

    # -- 乱序加载对拍 --
    rng = random.Random(SEED + 2)
    shuffled = rules[:]
    rng.shuffle(shuffled)
    router_b = Router(shuffled)
    sample = rng.sample(queries, 3000)
    order_check_ok = all(
        [h.rule_id for h in router.evaluate(p).matches]
        == [h.rule_id for h in router_b.evaluate(p).matches]
        for p in sample
    )

    stats = {
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "rules": {
            "total": N_RULES,
            "literal": N_LITERAL,
            "glob": N_GLOB,
            "escaped_literal": N_ESCAPED,
        },
        "build": {
            "seconds": round(build_s, 4),
            "tracemalloc_peak_bytes": build_peak,
        },
        "index_memory": {
            "bytes": index_bytes,
            "per_rule_bytes": round(index_bytes / N_RULES, 1),
        },
        "queries": {
            "total": N_QUERIES,
            "matched": n_match,
            "conflict": n_conflict,
            "no_match": n_no_match,
            "total_seconds": round(total_query_s, 4),
            "per_query_us": round(total_query_s / N_QUERIES * 1e6, 3),
            "p50_us": round(percentile(latencies_ns, 50) / 1000.0, 3),
            "p95_us": round(percentile(latencies_ns, 95) / 1000.0, 3),
            "p99_us": round(percentile(latencies_ns, 99) / 1000.0, 3),
            "max_us": round(latencies_ns[-1] / 1000.0, 3),
        },
        "order_invariance": {
            "shuffled_reload_queries_compared": len(sample),
            "identical": order_check_ok,
        },
    }

    lines = [
        "路由优先级匹配基准报告",
        "======================",
        "环境: Python %s / %s / %s" % (stats["environment"]["python"],
                                     stats["environment"]["platform"],
                                     stats["environment"]["machine"]),
        "",
        "规则规模: %d 条（字面 %d / 段内 glob %d / 转义字面 %d）"
        % (N_RULES, N_LITERAL, N_GLOB, N_ESCAPED),
        "构建耗时: %.3f s" % build_s,
        "构建期峰值分配(tracemalloc): %s" % fmt_bytes(build_peak),
        "索引常驻内存(index_memory_bytes): %s（约 %.1f B/条）"
        % (fmt_bytes(index_bytes), index_bytes / N_RULES),
        "",
        "查询规模: %d 次（命中 %d / 冲突 %d / 未命中 %d）"
        % (N_QUERIES, n_match, n_conflict, n_no_match),
        "查询总耗时: %.3f s" % total_query_s,
        "单次均值: %.3f us | p50 %.3f us | p95 %.3f us | p99 %.3f us | max %.3f us"
        % (stats["queries"]["per_query_us"], stats["queries"]["p50_us"],
           stats["queries"]["p95_us"], stats["queries"]["p99_us"],
           stats["queries"]["max_us"]),
        "",
        "乱序加载对拍: 规则乱序重建后抽样 %d 条查询，命中序列完全一致: %s"
        % (len(sample), "是" if order_check_ok else "否（严重错误）"),
    ]
    report = "\n".join(lines) + "\n"
    print(report)

    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    with open(os.path.join(ROOT, "reports", "benchmark_report.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(report)
    with open(os.path.join(ROOT, "reports", "benchmark_data.json"), "w",
              encoding="utf-8") as fh:
        json.dump(stats, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    if not order_check_ok:
        sys.exit("order invariance check FAILED")


if __name__ == "__main__":
    main()

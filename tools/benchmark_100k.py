"""100k-request benchmark: real wall-clock scheduling cost, injected virtual time.

The simulated workload (queueing + per-attempt downstream durations) is
pre-generated with a fixed seed and *virtual* time, so the benchmark measures
how long the scheduler itself takes on this machine, not real downstream
latency. Two budget tightness settings are reported.
"""

from __future__ import annotations

import json
import platform
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from timeout_budget import (  # noqa: E402
    Budget,
    Outcome,
    Stage,
    SliceTimeout,
    VirtualClock,
    run,
)

DATA = ROOT / "data"

N_REQUESTS = 100_000
N_STAGES = 4
WEIGHTS = (2, 2, 1, 1)
MAX_COSTS = 8  # scripted costs generated per stage (covers max max_attempts)
BACKOFF = 1
SEED = 20261005
CONFIGS = (
    # (total_budget, max_attempts)
    (100, 3),
    (50, 3),
    (100, 8),  # retries bounded by budget, not by the attempt cap
)


def generate_workload(n, rng):
    """Per-request: (queue_ticks, costs[stage][attempt])."""
    workload = [None] * n
    for i in range(n):
        queue = rng.choice([0, 0, 0, 0, 0, 1, 2, 5])
        costs = []
        for _stage in range(N_STAGES):
            stage_costs = [rng.randint(3, 45) for _ in range(MAX_COSTS)]
            costs.append(stage_costs)
        workload[i] = (queue, costs)
    return workload


def run_config(total, max_attempts, workload, count_outcomes):
    attempts_total = 0
    invariant_violations = 0

    def make_handler(costs, stage_index):
        def handler(ctx):
            cost = costs[stage_index][min(ctx.attempt - 1, MAX_COSTS - 1)]
            if cost <= ctx.slice_ticks:
                ctx.budget.clock.advance(cost)
                return stage_index
            # Cooperative downstream: times out exactly at its deadline.
            ctx.budget.clock.advance(ctx.slice_ticks)
            raise SliceTimeout("downstream deadline exceeded")

        return handler

    t_start = time.perf_counter()
    for queue, costs in workload:
        clock = VirtualClock(0)
        budget = Budget(total, clock)
        if queue:
            clock.advance(queue)
        stages = [
            Stage(
                "s{}".format(i),
                weight=WEIGHTS[i],
                max_attempts=max_attempts,
                backoff_ticks=BACKOFF,
                handler=make_handler(costs, i),
            )
            for i in range(N_STAGES)
        ]
        result = run(stages, budget)
        attempts_total += result.attempts_total
        count_outcomes[result.outcome] += 1
        if result.used > result.total or result.remaining < 0:
            invariant_violations += 1
    elapsed = time.perf_counter() - t_start
    return elapsed, attempts_total, invariant_violations


def micro_slice_for(iterations=400_000):
    budget = Budget(100, VirtualClock(0))
    t0 = time.perf_counter()
    for _ in range(iterations):
        budget.slice_for(2, 6)
    return time.perf_counter() - t0, iterations


def render_md(report):
    env = report["environment"]
    lines = [
        "# 10 万请求调度耗时基准",
        "",
        "负载由注入的虚拟时钟驱动，墙钟只用于度量**调度器自身**的开销；"
        "下游耗时/排队/backoff 均不真实睡眠。固定随机种子，可复现。",
        "",
        "- 请求数：{}；段数：{}（权重 {}）；每段最多 {} 次尝试；backoff {} tick".format(
            report["n_requests"],
            N_STAGES,
            WEIGHTS,
            "/".join(str(c[1]) for c in CONFIGS),
            BACKOFF,
        ),
        "- 下游尝试耗时：3–45 tick 均匀分布；排队：0/1/2/5 tick 加权抽样",
        "- Python：{}；平台：{} {}（{} 核）".format(
            env["python"],
            env["system"],
            env["machine"],
            env["cpus"],
        ),
        "",
        "| 总预算 | 每段最大尝试 | 墙钟总耗时 | 每请求 | 每次派发决策 | 派发尝试总数 | budget_exhausted 比例 | 其他结果 |",
        "|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for cfg in report["configs"]:
        others = ", ".join(
            "{} {:.2%}".format(k, v)
            for k, v in cfg["outcomes_pct"].items()
            if k != "budget_exhausted"
        )
        lines.append(
            "| {} tick | {} | {:.3f} s | {:.2f} µs | {:.3f} µs | {} | {:.2%} | {} |".format(
                cfg["total"],
                cfg["max_attempts"],
                cfg["wall_seconds"],
                cfg["us_per_request"],
                cfg["us_per_attempt"],
                cfg["attempts_total"],
                cfg["outcomes_pct"]["budget_exhausted"],
                others,
            )
        )
    violations = sum(c["invariant_violations"] for c in report["configs"])
    micro = report["micro_slice_for"]
    lines += [
        "",
        "不变量校验：全部 {} 次运行 `used <= total` 且 `remaining >= 0`，违规 {} 次。".format(
            report["n_requests"] * len(report["configs"]), violations
        ),
        "",
        "## 纯切片决策微基准",
        "",
        "`Budget.slice_for(2, 6)` 连续调用 {} 次：{:.4f} s，即每次 {:.0f} ns"
        "（无下游、无重试，仅算预算算术）。".format(
            micro["iterations"],
            micro["seconds"],
            micro["seconds"] / micro["iterations"] * 1e9,
        ),
        "",
        "复现：`python3 tools/benchmark_100k.py`（原始数据：`data/benchmark_100k.json`）。",
        "",
    ]
    return "\n".join(lines)


def main():
    rng = __import__("random").Random(SEED)
    workload = generate_workload(N_REQUESTS, rng)

    configs = []
    for total, max_attempts in CONFIGS:
        counts = Counter({o: 0 for o in Outcome})
        elapsed, attempts_total, violations = run_config(
            total, max_attempts, workload, counts
        )
        n = sum(counts.values())
        configs.append(
            {
                "total": total,
                "max_attempts": max_attempts,
                "wall_seconds": elapsed,
                "us_per_request": elapsed / n * 1e6,
                "us_per_attempt": elapsed / attempts_total * 1e6,
                "attempts_total": attempts_total,
                "attempts_per_request": attempts_total / n,
                "invariant_violations": violations,
                "outcomes": {o.value: counts[o] for o in Outcome},
                "outcomes_pct": {
                    o.value: counts[o] / n for o in Outcome
                },
            }
        )

    micro_seconds, micro_iters = micro_slice_for()
    report = {
        "n_requests": N_REQUESTS,
        "environment": {
            "python": platform.python_version(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpus": __import__("os").cpu_count(),
        },
        "workload": {
            "stages": N_STAGES,
            "weights": list(WEIGHTS),
            "max_attempts_per_config": [c[1] for c in CONFIGS],
            "backoff_ticks": BACKOFF,
            "attempt_cost_uniform": [3, 45],
            "queue_choices": [0, 0, 0, 0, 0, 1, 2, 5],
            "seed": SEED,
        },
        "configs": configs,
        "micro_slice_for": {
            "iterations": micro_iters,
            "seconds": micro_seconds,
        },
    }
    (DATA / "benchmark_100k.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = render_md(report)
    (DATA / "benchmark_100k.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()

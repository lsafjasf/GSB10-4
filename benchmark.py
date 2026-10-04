"""10 万次请求的调度耗时与预算耗尽比例基准：python3 benchmark.py

时间由 FakeClock 注入（虚拟毫秒），因此 10 万次请求瞬间跑完；
"调度耗时"用 perf_counter 测量 run_chain 自身（纯 Python 切分/记账逻辑）的墙钟开销。
结果写入 data/benchmark_100k.json。
"""

import json
import os
import platform
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from budget_chain import FakeClock, Stage, TimeBudget, run_chain


N = 100_000
BUDGET_MS = 100.0
BASE_WORK = [8.0, 10.0, 12.0]  # 三段正常工作耗时基线（等权三段各约 1/3 预算，需留重试余量）


def make_handler(clock, base_work, hang):
    def handler(ctx, grant):
        if hang:
            clock.sleep(grant)  # 下游挂死：合作地用满授权后报超时
            raise TimeoutError("downstream timeout")
        work = base_work + random.uniform(-5.0, 5.0)
        if work > grant:
            clock.sleep(grant)
            raise TimeoutError("downstream timeout")
        clock.sleep(work)
        return "ok"

    return handler


def one_request(rng, hang_stage):
    clock = FakeClock(0)
    queue = rng.uniform(0.0, 10.0)
    budget = TimeBudget.start(BUDGET_MS, clock=clock, sleep=clock.sleep)
    stages = [
        Stage(
            name=f"s{i}",
            handler=make_handler(clock, BASE_WORK[i], hang_stage == i),
            max_attempts=2,
            max_attempt_ms=60.0,
            backoff_base_ms=5.0,
            backoff_factor=2.0,
        )
        for i in range(3)
    ]
    t0 = time.perf_counter()
    result = run_chain(budget, stages, queue_wait_ms=queue)
    sched = time.perf_counter() - t0
    return result, sched


def main():
    rng = random.Random(42)
    counts = {"ok": 0, "exhausted": 0, "stage_timeout": 0, "stage_error": 0}
    reasons = {}
    elapsed_values = []
    max_elapsed = 0.0
    sched_total = 0.0

    # 场景分布：70% 正常；每段各 10% 概率挂死（验证首/中/末段超时与重试切分）
    t0 = time.perf_counter()
    for _ in range(N):
        kind = rng.random()
        hang_stage = -1
        if kind >= 0.7:
            hang_stage = min(int((kind - 0.7) * 10), 2)
        result, sched = one_request(rng, hang_stage)
        sched_total += sched
        counts[result.status] += 1
        reasons[result.reason or "ok"] = reasons.get(result.reason or "ok", 0) + 1
        elapsed_values.append(result.elapsed_ms)
        max_elapsed = max(max_elapsed, result.elapsed_ms)
    wall = time.perf_counter() - t0

    elapsed_values.sort()
    timeout_like = counts["exhausted"] + counts["stage_timeout"]
    report = {
        "requests": N,
        "budget_ms_per_request": BUDGET_MS,
        "scenario_mix": "70% 正常；每段 10% 挂死；排队 U(0,10)ms；各段 2 次尝试+退避",
        "wall_time_s": round(wall, 4),
        "per_request_us": round(wall / N * 1e6, 3),
        "run_chain_only_us": round(sched_total / N * 1e6, 3),
        "status_counts": counts,
        "exhausted_ratio": round(counts["exhausted"] / N, 6),
        "timeout_like_ratio": round(timeout_like / N, 6),
        "ok_ratio": round(counts["ok"] / N, 6),
        "reason_counts": reasons,
        "virtual_elapsed_ms": {
            "max": round(max_elapsed, 4),
            "p50": round(elapsed_values[N // 2], 4),
            "p95": round(elapsed_values[int(N * 0.95)], 4),
            "p99": round(elapsed_values[int(N * 0.99)], 4),
        },
        "invariant": f"max virtual elapsed {max_elapsed:.4f}ms <= {BUDGET_MS}ms"
                     f"（任何请求都未超过总预算，重试未放大总时长）",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }

    print(f"请求数: {N}")
    print(f"墙钟总耗时: {wall:.3f}s，单请求调度耗时: {wall/N*1e6:.2f}us")
    print(f"其中 run_chain 切分逻辑本身: {sched_total/N*1e6:.2f}us/请求")
    print(f"状态分布: {counts}")
    print(f"预算耗尽比例 exhausted: {counts['exhausted']/N:.4%}")
    print(f"超时类合计 (exhausted+stage_timeout): {timeout_like/N:.4%}")
    print(f"成功比例 ok: {counts['ok']/N:.4%}")
    print(f"原因明细: {reasons}")
    print(f"虚拟耗时 p50/p95/p99/max = {report['virtual_elapsed_ms']}")
    print(report["invariant"])
    assert max_elapsed <= BUDGET_MS + 1e-9, "存在请求超出总预算！"

    out = os.path.join(os.path.dirname(__file__), "data", "benchmark_100k.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"已写入 {out}")


if __name__ == "__main__":
    main()

"""10 万次超时场景压测：响应延迟分布 + 调度耗时。

运行：python3 bench.py [--n 100000] [--seed 20261005] [--out reports]

设计：
- 每次迭代构造一个纯工作型任务（无资源），工作切成 steps 片，
  每片耗时 slice_dt ∈ [0.01, 0.10)，片间是检查点；
- deadline 均匀落在 [0, total_work] 上，因此 100% 触发超时；
- 记录响应延迟（信号到期 -> 检查点响应）与每次 run() 的墙钟耗时；
- 同一随机种子跑两遍，验证延迟序列逐位一致（可复现性对拍）；
- 不变量：全部 TIMEOUT、0 <= latency <= max(slice_dt)、零资源泄漏。
"""

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coop_timeout import Kind, run  # noqa: E402


def make_work_task(steps: int, slice_dt: float):
    def task(ctx):
        ctx.enter_phase("WORK")
        ctx.work(steps * slice_dt, steps=steps)
        ctx.enter_phase("SUBMIT")
        return ctx.complete("done")

    return task


def gen_scenarios(n: int, seed: int):
    rng = random.Random(seed)
    scenarios = []
    for _ in range(n):
        steps = rng.randint(50, 200)
        slice_dt = 0.01 + rng.random() * 0.09
        total = steps * slice_dt
        deadline = rng.random() * total
        scenarios.append((steps, slice_dt, deadline))
    return scenarios


def execute(scenarios):
    """执行一遍，返回 (latencies, wall_ns_list, counters)。"""
    latencies = []
    wall_ns = []
    events = 0
    checkpoints = 0
    for steps, slice_dt, deadline in scenarios:
        t0 = time.perf_counter_ns()
        res = run(make_work_task(steps, slice_dt), deadline=deadline)
        t1 = time.perf_counter_ns()
        o = res.outcome
        assert o.kind is Kind.TIMEOUT, f"expected TIMEOUT, got {o.kind}"
        assert o.latency is not None and 0.0 <= o.latency <= slice_dt + 1e-9, o
        latencies.append(o.latency)
        wall_ns.append(t1 - t0)
        events += res.clock.events_fired
        checkpoints += res.ctx.checkpoints
    counters = {"events_fired": events, "checkpoints": checkpoints}
    return latencies, wall_ns, counters


def percentiles(sorted_data, ps):
    n = len(sorted_data)
    out = {}
    for p in ps:
        if n == 1:
            out[p] = sorted_data[0]
            continue
        rank = p / 100.0 * (n - 1)
        lo = int(rank)
        hi = min(lo + 1, n - 1)
        frac = rank - lo
        out[p] = sorted_data[lo] * (1 - frac) + sorted_data[hi] * frac
    return out


def histogram(sorted_data, buckets=20):
    lo, hi = sorted_data[0], sorted_data[-1]
    if hi == lo:
        return [(lo, hi, len(sorted_data))]
    width = (hi - lo) / buckets
    counts = [0] * buckets
    for v in sorted_data:
        idx = min(int((v - lo) / width), buckets - 1)
        counts[idx] += 1
    return [(lo + i * width, lo + (i + 1) * width, c) for i, c in enumerate(counts)]


def stats_line(name, data, unit, scale=1.0):
    s = sorted(data)
    ps = percentiles(s, [50, 90, 99, 99.9])
    lines = [
        f"[{name}] n={len(s)}",
        f"  min={s[0]*scale:.4f}{unit}  p50={ps[50]*scale:.4f}{unit}  "
        f"p90={ps[90]*scale:.4f}{unit}  p99={ps[99]*scale:.4f}{unit}  "
        f"p99.9={ps[99.9]*scale:.4f}{unit}  max={s[-1]*scale:.4f}{unit}",
        f"  mean={statistics.fmean(s)*scale:.4f}{unit}  "
        f"stdev={statistics.pstdev(s)*scale:.4f}{unit}",
    ]
    return lines, {
        "n": len(s),
        "min": s[0] * scale,
        "p50": ps[50] * scale,
        "p90": ps[90] * scale,
        "p99": ps[99] * scale,
        "p99.9": ps[99.9] * scale,
        "max": s[-1] * scale,
        "mean": statistics.fmean(s) * scale,
        "stdev": statistics.pstdev(s) * scale,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100_000)
    ap.add_argument("--seed", type=int, default=20261005)
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()

    scenarios = gen_scenarios(args.n, args.seed)

    lat1, wall1, counters = execute(scenarios)
    lat2, _, _ = execute(gen_scenarios(args.n, args.seed))
    reproducible = lat1 == lat2

    lines = []
    lines.append(f"# 10 万次超时场景压测 (n={args.n}, seed={args.seed})")
    lines.append(f"# 可复现性对拍: 两遍延迟序列逐位一致 = {reproducible}")
    lines.append(f"# 检查点总数={counters['checkpoints']}  事件触发总数={counters['events_fired']}")
    lines.append("")

    lat_lines, lat_stats = stats_line("响应延迟（虚拟时间单位）", lat1, "")
    lines += lat_lines
    lines.append("")
    lines.append("响应延迟直方图（20 桶）:")
    hist = histogram(sorted(lat1))
    max_c = max(c for _, _, c in hist)
    for lo, hi, c in hist:
        bar = "#" * max(1, int(60 * c / max_c)) if c else ""
        lines.append(f"  [{lo:7.4f}, {hi:7.4f}) {c:>7} {bar}")
    lines.append("")

    wall_us = [w / 1000.0 for w in wall1]
    wall_lines, wall_stats = stats_line("调度耗时 run() 墙钟", wall_us, "us")
    lines += wall_lines
    total_ms = sum(wall1) / 1e6
    lines.append(f"  总调度耗时={total_ms:.1f}ms  吞吐={args.n / (total_ms / 1000):,.0f} 次/秒")
    lines.append("")
    lines.append(f"不变量: 100% TIMEOUT, 0 <= latency <= max(slice_dt)=0.1, 资源泄漏=0  -- 全部通过")

    report = "\n".join(lines)
    print(report)

    out_dir = Path(args.out)
    out_dir.mkdir(exist_ok=True)
    (out_dir / "benchmark_100k.txt").write_text(report + "\n", encoding="utf-8")
    (out_dir / "benchmark_100k.json").write_text(
        json.dumps(
            {
                "n": args.n,
                "seed": args.seed,
                "reproducible": reproducible,
                "counters": counters,
                "latency": lat_stats,
                "scheduling_us": wall_stats,
                "total_scheduling_ms": total_ms,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if not reproducible:
        sys.exit("FAIL: 两遍结果不一致")


if __name__ == "__main__":
    main()

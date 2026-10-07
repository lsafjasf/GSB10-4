"""一键基准：四类场景摘要 + 开启/关闭监控开销对比 + 100 万回调规模。

用法：python scripts/benchmark.py
时间注入：场景与百万轨迹使用虚拟时钟/确定性合成；
真实 wall clock 仅用于测量分析器自身耗时（perf_counter）。
"""

import json
import os
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fairloop import FairLoop, FairnessMonitor, VirtualClock, analyze
from fairloop.scenarios import (
    scenario_short_tasks, scenario_one_hog,
    scenario_periodic_medium, scenario_idle_slices,
)
from tests.test_million import synthesize, N as MILLION


SCENARIOS = [
    ("all-short", scenario_short_tasks),
    ("one-hog", scenario_one_hog),
    ("periodic-medium", scenario_periodic_medium),
    ("idle-slices", scenario_idle_slices),
]


def fmt(sec):
    return f"{sec * 1000:.3f}ms"


def main():
    print("== 四类场景（分析器输出摘要） ==")
    for name, fn in SCENARIOS:
        t0 = time.perf_counter()
        report = analyze(fn())
        dt = time.perf_counter() - t0
        top = report.culprits[0] if report.culprits else None
        print(f"\n[{name}] runs={report.total_runs} analyze={fmt(dt)}")
        print(f"  wall={report.wall_time:.3f}s busy={report.busy_time:.3f}s "
              f"idle={report.idle_time:.3f}s util={report.utilization:.3f}")
        print(f"  duration p50={fmt(report.duration['p50'])} "
              f"p90={fmt(report.duration['p90'])} p99={fmt(report.duration['p99'])} "
              f"max={fmt(report.duration['max'])}")
        print(f"  delay    p50={fmt(report.delay['p50'])} "
              f"p90={fmt(report.delay['p90'])} p99={fmt(report.delay['p99'])} "
              f"max={fmt(report.delay['max'])}")
        if top and top.total_induced_wait > 0:
            print(f"  top culprit: {top.name} @ {top.site} "
                  f"runs={top.runs} max_occ={fmt(top.max_occupied)} "
                  f"induced={top.total_induced_wait:.3f}s victims={top.victims}")
        else:
            print("  top culprit: none")

    print("\n== 监控开销对比（200,000 次回调，5 次取最小值） ==")
    def loop_time(with_monitor, n=200_000):
        clock = VirtualClock()
        mon = FairnessMonitor() if with_monitor else None
        loop = FairLoop(clock, mon)
        for i in range(n):
            loop.schedule(lambda: 0.001, name=f"t#{i}", site="bench:tick")
        t0 = time.perf_counter()
        loop.drain()
        return time.perf_counter() - t0

    base = min(loop_time(False) for _ in range(5))
    mon = min(loop_time(True) for _ in range(5))
    print(f"  off={base:.3f}s on={mon:.3f}s ratio={mon / base:.2f}x "
          f"extra={(mon - base) / 200_000 * 1e9:.0f}ns/event")

    print(f"\n== 100 万回调规模（n={MILLION}，确定性合成） ==")
    records = synthesize()
    t0 = time.perf_counter()
    report = analyze(records)
    elapsed = time.perf_counter() - t0
    tracemalloc.start()
    analyze(records)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    hog = next(c for c in report.culprits if c.name == "hog")
    print(f"  analyze={elapsed:.2f}s（无 tracer） "
          f"peak_mem={peak / 1024 / 1024:.1f}MiB（tracemalloc，含轨迹）")
    print(f"  hog: victims={hog.victims} induced={hog.total_induced_wait:.1f}s")


if __name__ == "__main__":
    main()

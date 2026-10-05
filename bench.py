"""100 万回调基准：分析耗时、内存峰值、开/关监控开销、确定性对拍。

运行：python3 bench.py
"""

import gc
import time
import tracemalloc

from fairloop import EventLoop, FakeClock, FairnessMonitor, analyze

N = 1_000_000
BATCH = 10_000
US = 1_000
MS = 1_000_000
SHORT_SITES = [("short_%d" % i, "bench.py:%d" % (30 + i)) for i in range(5)]
HEAVY_SITE = ("heavy_batch", "bench.py:88")
FEEDER_SITE = ("feeder", "bench.py:90")


def build(with_monitor, n=N):
    clock = FakeClock()
    monitor = FairnessMonitor(clock.time_ns) if with_monitor else None
    loop = EventLoop(clock.time_ns, clock.advance, monitor)
    state = {"i": 0}

    def work(ns):
        def _w():
            clock.advance(ns)
        return _w

    def feeder():
        end = min(state["i"] + BATCH, n)
        while state["i"] < end:
            i = state["i"]
            if i % 1000 == 999:  # 每 1000 个里塞一个 5ms 中等任务
                loop.call_soon(work(5 * MS), name=HEAVY_SITE[0],
                               site=HEAVY_SITE[1])
            else:
                name, site = SHORT_SITES[i % 5]
                loop.call_soon(work(1 * US + (i % 200) * 5 * US),
                               name=name, site=site)
            state["i"] += 1
        if state["i"] < n:
            loop.call_soon(feeder, name=FEEDER_SITE[0], site=FEEDER_SITE[1])

    loop.call_soon(feeder, name=FEEDER_SITE[0], site=FEEDER_SITE[1])
    return clock, monitor, loop, state


def timed_run(with_monitor, runs=3):
    best = float("inf")
    last_monitor = None
    for _ in range(runs):
        gc.collect()
        _, monitor, loop, _ = build(with_monitor)
        t0 = time.perf_counter()
        loop.run()
        dt = time.perf_counter() - t0
        best = min(best, dt)
        last_monitor = monitor
    return best, last_monitor


def deep_size(obj, seen=None):
    if seen is None:
        seen = set()
    if id(obj) in seen:
        return 0
    seen.add(id(obj))
    size = sys_getsizeof(obj)
    if isinstance(obj, dict):
        for k, v in obj.items():
            size += deep_size(k, seen) + deep_size(v, seen)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            size += deep_size(item, seen)
    return size


import sys
sys_getsizeof = sys.getsizeof


def monitor_footprint(monitor):
    """监控器自身驻留：2 个直方图 + K 个注册点聚合（不含任何逐事件明细）。"""
    return (
        deep_size(monitor.block_hist)
        + deep_size(monitor.delay_hist)
        + deep_size(monitor._stats)
        + deep_size(monitor._open)
    )


def main():
    print("=" * 72)
    print("事件循环公平性检查器 — 100 万回调基准（Python %s）"
          % sys.version.split()[0])
    print("=" * 72)

    # 1) 开/关监控耗时对比 ------------------------------------------------
    off, _ = timed_run(False)
    on, monitor = timed_run(True)
    print("\n[耗时对比] %d 个回调（3 次取最小值）" % N)
    print("  监控关闭 : %.3f s" % off)
    print("  监控开启 : %.3f s" % on)
    print("  附加开销 : %.1f%%（每回调 %.0f ns，硬上界见测试 < 20µs）"
          % ((on / off - 1) * 100, (on - off) / N * 1e9))

    # 2) 100 万事件采集的内存峰值 ----------------------------------------
    gc.collect()
    tracemalloc.start()
    _, mon_peak, loop_peak, _ = build(True)
    loop_peak.run()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print("\n[内存] 采集 100 万回调（tracemalloc）")
    print("  结束后驻留 : %.2f MiB" % (current / 2**20))
    print("  峰值       : %.2f MiB" % (peak / 2**20))

    # 3) analyze() 分析耗时与结果内存 ------------------------------------
    _, mon, loop, _ = build(True)
    loop.run()
    best = float("inf")
    report = None
    for _ in range(5):
        t0 = time.perf_counter()
        report = analyze(mon)
        best = min(best, time.perf_counter() - t0)
    tracemalloc.start()
    r = analyze(mon)
    _, report_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print("\n[分析] analyze() 纯函数分析")
    print("  耗时       : %.3f ms（5 次取最小；注册点种类 K=%d，与事件数无关）"
          % (best * 1e3, len(r.offenders)))
    print("  结果对象   : %.1f KiB" % (report_peak / 2**10))
    print("  监控器驻留 : %.1f KiB（2×64 桶直方图 + K 行聚合，无逐事件明细）"
          % (monitor_footprint(mon) / 2**10))

    # 4) 有界内存验证：事件数 ×10，监控器驻留不变 -------------------------
    _, mon_small, loop_small, _ = build(True, n=100_000)
    loop_small.run()
    small_fp = monitor_footprint(mon_small)
    big_fp = monitor_footprint(mon)
    print("\n[有界性] 10 万事件驻留 %d B vs 100 万事件驻留 %d B（注册点相同"
          "时应相等）" % (small_fp, big_fp))
    assert small_fp == big_fp, "监控器内存不应随事件数增长！"

    # 5) 确定性对拍 -------------------------------------------------------
    d1 = analyze(mon).to_dict()
    d2 = analyze(mon).to_dict()
    assert d1 == d2
    print("[对拍] 同记录分析两次：完全一致（events=%d, busy=%.3fs, idle=%.3fs）"
          % (d1["events"], d1["busy_ns"] / 1e9, d1["idle_ns"] / 1e9))

    # 6) 拖累清单 ---------------------------------------------------------
    print("\n[拖累清单] 按总占用降序（top 8）")
    print("  %-12s %-14s %8s %12s %12s %8s"
          % ("名称", "注册点", "次数", "总占用(s)", "单次最大", "占比"))
    for o in r.offenders[:8]:
        print("  %-12s %-14s %8d %12.3f %12.3f %7.2f%%"
              % (o.name, o.site, o.invocations, o.total_block_ns / 1e9,
                 o.max_block_ns / 1e9, o.block_share * 100))

    b, d = r.block, r.delay
    print("\n[度量] 占用/延迟分位数（保守桶下界）")
    print("  占用(ms): mean=%.3f p50=%.3f p90=%.3f p99=%.3f max=%.3f"
          % (b.mean_ns / 1e6, b.p50_ns / 1e6, b.p90_ns / 1e6,
             b.p99_ns / 1e6, b.max_ns / 1e6))
    print("  延迟(ms): mean=%.3f p50=%.3f p90=%.3f p99=%.3f max=%.3f"
          % (d.mean_ns / 1e6, d.p50_ns / 1e6, d.p90_ns / 1e6,
             d.p99_ns / 1e6, d.max_ns / 1e6))
    print("=" * 72)


if __name__ == "__main__":
    main()

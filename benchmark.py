"""性能基准:1000 个订阅者,100 万次分发。

运行: python3 benchmark.py          # 全量(全广播场景约 30~60s)
      python3 benchmark.py --quick  # 快速冒烟(10 万次分发)

内存口径:
  - RSS 峰值增量: resource.ru_maxrss(进程高水位),基准在"订阅完成后"取值,
    因此反映的是纯分发阶段的内存增长(应接近 0:分发路径无持续分配);
  - Python 分配峰值: tracemalloc,在独立的较短工作负载上测量,
    避免追踪开销污染计时结果。
"""

import argparse
import gc
import platform
import resource
import sys
import time
import tracemalloc

from mixed_dispatch import Topic

N_SUBSCRIBERS = 1000


def fresh_topic(n=N_SUBSCRIBERS):
    topic = Topic()
    state = {"n": 0}

    def handler(msg):
        state["n"] += 1

    tokens = [topic.subscribe(handler) for _ in range(n)]
    return topic, state, tokens


def rss_kb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def time_scenario(name, n_dispatches, mode):
    topic, state, _ = fresh_topic()
    gc.collect()
    baseline_rss = rss_kb()
    t0 = time.perf_counter()
    if mode == "unicast":
        for i in range(n_dispatches):
            topic.publish_unicast(i)
    elif mode == "broadcast":
        for i in range(n_dispatches):
            topic.publish_broadcast(i)
    elif mode == "mixed":
        for i in range(n_dispatches // 2):
            topic.publish_broadcast(i)
            topic.publish_unicast(i)
    elapsed = time.perf_counter() - t0
    peak_rss_delta = rss_kb() - baseline_rss
    deliveries = state["n"]
    return {
        "name": name,
        "mode": mode,
        "dispatches": n_dispatches,
        "deliveries": deliveries,
        "elapsed_s": elapsed,
        "dispatches_per_s": n_dispatches / elapsed,
        "deliveries_per_s": deliveries / elapsed,
        "rss_growth_kb": peak_rss_delta,
    }


def allocation_peak(n_broadcast=1000, n_unicast=100000):
    """tracemalloc:分发路径的 Python 堆分配峰值。"""
    topic, _, _ = fresh_topic()
    tracemalloc.start()
    for i in range(n_unicast):
        topic.publish_unicast(i)
    for i in range(n_broadcast):
        topic.publish_broadcast(i)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"unicast": n_unicast, "broadcast": n_broadcast,
            "peak_bytes": peak}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    n = 100_000 if args.quick else 1_000_000

    lines = []
    def out(s=""):
        print(s)
        lines.append(s)

    out("# benchmark: %d subscribers" % N_SUBSCRIBERS)
    out("# python %s | %s %s"
        % (platform.python_version(), platform.system(), platform.release()))
    out("# 每个订阅者的 handler 都执行一次计数器自增(真实工作,非纯空函数)")

    for name, mode in (("all_unicast", "unicast"),
                       ("all_broadcast", "broadcast"),
                       ("mixed_50_50", "mixed")):
        r = time_scenario(name, n, mode)
        out("\n[%s] %d dispatches (%s)" % (r["name"], r["dispatches"], mode))
        out("  elapsed              : %.3f s" % r["elapsed_s"])
        out("  actual deliveries    : %d" % r["deliveries"])
        out("  dispatches/sec       : " + format(round(r["dispatches_per_s"]), "_d"))
        out("  deliveries/sec       : " + format(round(r["deliveries_per_s"]), "_d"))
        out("  RSS growth, dispatch : %d KiB" % r["rss_growth_kb"])

    if args.quick:
        alloc = allocation_peak(n_broadcast=100, n_unicast=10000)
    else:
        alloc = allocation_peak()
    out("\n[tracemalloc] %d unicast + %d broadcast dispatches"
        % (alloc["unicast"], alloc["broadcast"]))
    out("  Python heap peak     : %d bytes (%.1f KiB)"
        % (alloc["peak_bytes"], alloc["peak_bytes"] / 1024))
    out("  baseline: 1000 subscribers + topic 之后,RSS 增量为 0 表示"
        "百万次分发没有持续占用内存")

    with open("benchmark_results.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())

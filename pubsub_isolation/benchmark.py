"""基准：1000 个订阅者、10 万次发布，输出耗时与内存峰值。运行：python3 benchmark.py"""

import gc
import resource
import time
import tracemalloc

from pubsub import EventBus

N_SUBS = 1000
N_PUBLISH = 100_000


def peak_rss_mb() -> float:
    # Linux: ru_maxrss 单位为 KB
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main() -> None:
    bus = EventBus()
    counter = 0

    def handler(e):
        nonlocal counter
        counter += e

    def handler2(e):
        pass

    for _ in range(N_SUBS):
        bus.subscribe("bench", handler)

    gc.collect()
    baseline_rss = peak_rss_mb()
    start = time.perf_counter()

    for i in range(N_PUBLISH):
        result = bus.publish("bench", 1)
        assert result.ok

    elapsed = time.perf_counter() - start

    # 单独用 tracemalloc 量一轮稳定态 Python 堆增量（避开 1 亿次调用的 trace 开销）
    tracemalloc.start()
    bus2 = EventBus()
    for _ in range(N_SUBS):
        bus2.subscribe("bench", handler2)
    for i in range(10_000):
        bus2.publish("bench", 1)
    _, traced_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    total_calls = N_SUBS * N_PUBLISH
    print(f"subscribers        : {N_SUBS}")
    print(f"publishes          : {N_PUBLISH}")
    print(f"total deliveries   : {total_calls:,} (counter={counter:,})")
    print(f"elapsed            : {elapsed:.3f} s")
    print(f"per publish        : {elapsed / N_PUBLISH * 1e6:.2f} us")
    print(f"per delivery       : {elapsed / total_calls * 1e9:.1f} ns")
    print(f"throughput         : {total_calls / elapsed / 1e6:.2f} M deliveries/s")
    print(f"peak python mem    : {traced_peak / 1024 / 1024:.2f} MiB (tracemalloc)")
    print(f"peak process RSS   : {peak_rss_mb():.2f} MiB (ru_maxrss, baseline {baseline_rss:.2f} MiB)")


if __name__ == "__main__":
    main()

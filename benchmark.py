"""1,000,000 enqueue/dequeue benchmark: scheduling time and peak memory.

Two passes:
  1. Timing pass with perf_counter + process peak RSS (resource.ru_maxrss).
  2. Memory pass under tracemalloc for Python-allocated peak bytes.

Two workloads:
  A. bulk      - enqueue 1e6 mixed-priority tasks, then drain 1e6
  B. steady    - interleave enqueue/dequeue (working set stays small)
Stdlib only.
"""

import gc
import random
import resource
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from priority_queue import PriorityQueue, Task

N = 1_000_000
AGING = 100.0


def rss_mb() -> float:
    # ru_maxrss is kilobytes on Linux.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def workload_bulk(trace: bool):
    rng = random.Random(1)
    q = PriorityQueue(aging_interval=AGING, clock=lambda: t0)
    t0 = 0.0
    base_rss = rss_mb()
    t_en = t_de = 0.0
    peak_heap = 0
    if trace:
        tracemalloc.start()
    t_start = time.perf_counter()
    for i in range(N):
        t0 = i  # injected monotone time
        q.enqueue(Task(task_id=i, priority=rng.randrange(10)))
        peak_heap = max(peak_heap, q.heap_size)
    t_en = time.perf_counter() - t_start
    t_start = time.perf_counter()
    while q.dequeue() is not None:
        pass
    t_de = time.perf_counter() - t_start
    py_peak = 0
    if trace:
        _, py_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    peak_rss = rss_mb()
    return t_en, t_de, peak_heap, peak_rss - base_rss, peak_rss, py_peak


def workload_steady(trace: bool):
    rng = random.Random(2)
    q = PriorityQueue(aging_interval=AGING, clock=lambda: t0)
    t0 = 0.0
    if trace:
        tracemalloc.start()
    t_start = time.perf_counter()
    for i in range(N):
        t0 = i
        q.enqueue(Task(task_id=i, priority=rng.randrange(10)))
        q.dequeue()
    t_total = time.perf_counter() - t_start
    py_peak = 0
    if trace:
        _, py_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return t_total, q.heap_size, py_peak


def fmt(t):
    return f"{t:8.3f}s"


def main() -> None:
    print(f"=== {N:,} enqueue/dequeue benchmark (Python {sys.version.split()[0]}) ===")

    gc.collect()
    print("\n[timing pass] workload A: bulk enqueue -> drain")
    t_en, t_de, peak_heap, d_rss, peak_rss, _ = workload_bulk(trace=False)
    ops = 2 * N
    print(f"  enqueue {N:>9,}: {fmt(t_en)}   {N / t_en:>12,.0f} ops/s")
    print(f"  dequeue {N:>9,}: {fmt(t_de)}   {N / t_de:>12,.0f} ops/s")
    print(f"  total   {ops:>9,}: {fmt(t_en + t_de)}   "
          f"{ops / (t_en + t_de):>12,.0f} ops/s")
    print(f"  peak heap length : {peak_heap:,} tasks")
    print(f"  peak RSS (delta) : {d_rss:,.0f} MiB   (process peak {peak_rss:,.0f} MiB)")

    gc.collect()
    print("\n[timing pass] workload B: interleaved enqueue/dequeue")
    t_total, heap_len, _ = workload_steady(trace=False)
    print(f"  total {2 * N:>9,}: {fmt(t_total)}   "
          f"{2 * N / t_total:>12,.0f} ops/s   final heap {heap_len}")

    gc.collect()
    print("\n[memory pass] tracemalloc Python-allocated peak, workload A")
    _, _, _, _, _, py_peak = workload_bulk(trace=True)
    print(f"  Python peak allocations at full depth: "
          f"{py_peak / 1024 / 1024:,.0f} MiB "
          f"({py_peak / N:,.0f} bytes/task)")

    print("\n[memory pass] tracemalloc Python-allocated peak, workload B")
    _, _, py_peak_s = workload_steady(trace=True)
    print(f"  Python peak allocations (steady depth): "
          f"{py_peak_s / 1024 / 1024:,.1f} MiB")


if __name__ == "__main__":
    main()

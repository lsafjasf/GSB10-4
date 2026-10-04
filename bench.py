"""性能基准：100 万次入队/出队的耗时与队列内存峰值。

运行: python3 bench.py
"""

import random
import tracemalloc
from time import perf_counter

from fair_queue import AgingPriorityQueue

N = 1_000_000


def make_workload():
    rng = random.Random(7)
    return [rng.randrange(8) for _ in range(N)]


def bench_time(prios):
    q = AgingPriorityQueue(num_priorities=8, aging_interval=60.0)
    t0 = perf_counter()
    for i in range(N):
        q.enqueue(i, priority=prios[i])
    t1 = perf_counter()
    for _ in range(N):
        q.dequeue()
    t2 = perf_counter()
    enq, deq = t1 - t0, t2 - t1
    print("[time]  enqueue 1,000,000: %6.2f s  (%8.0f ops/s)" % (enq, N / enq))
    print("[time]  dequeue 1,000,000: %6.2f s  (%8.0f ops/s)" % (deq, N / deq))
    print("[time]  total:             %6.2f s" % (enq + deq))


def bench_memory(prios):
    q = AgingPriorityQueue(num_priorities=8, aging_interval=60.0)
    tracemalloc.start()
    for i in range(N):
        q.enqueue(i, priority=prios[i])
    current, peak = tracemalloc.get_traced_memory()
    print("[mem]   live after 1M enqueues: %8.1f MiB" % (current / 2**20))
    print("[mem]   peak during 1M enqueues: %8.1f MiB" % (peak / 2**20))
    tracemalloc.reset_peak()
    for _ in range(N):
        q.dequeue()
    current, peak = tracemalloc.get_traced_memory()
    print("[mem]   live after 1M dequeues:  %8.1f MiB" % (current / 2**20))
    tracemalloc.stop()


if __name__ == "__main__":
    prios = make_workload()
    bench_time(prios)
    bench_memory(prios)

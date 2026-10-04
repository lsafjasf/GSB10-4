"""基准：100 万次派生 + 读取的耗时与内存。

运行: python3 bench_corolocal.py [N]   （默认 N=1_000_000）
"""

import asyncio
import resource
import sys
import time
import tracemalloc

import corolocal as cl


async def child(i):
    return cl.get("k")


async def bench_spawn_read(n):
    cl.set("k", 1)
    cl.set("obj", {"v": [1, 2, 3]})   # 让深拷贝有真实工作量
    tracemalloc.start()
    t0 = time.perf_counter()
    acc = 0
    for i in range(n):
        acc += await cl.spawn(child(i))   # 顺序派生：内存有界
    dt = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert acc == n, acc
    return dt, peak


async def bench_concurrent(n):
    """并发批式派生：同时存活 n 个任务，看峰值内存。"""
    cl.set("k", 1)
    tracemalloc.start()
    t0 = time.perf_counter()
    tasks = [cl.spawn(child(i)) for i in range(n)]
    acc = sum(await asyncio.gather(*tasks))
    dt = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert acc == n, acc
    return dt, peak


def fmt_mb(b):
    return f"{b / 1024 / 1024:.2f} MB"


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000

    dt, peak = asyncio.run(bench_spawn_read(n))
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    print(f"[顺序派生]  N={n:,}")
    print(f"  总耗时        : {dt:.3f} s")
    print(f"  单次派生+读取 : {dt / n * 1e6:.2f} µs")
    print(f"  峰值内存(tracemalloc): {fmt_mb(peak)}")
    print(f"  进程峰值 RSS  : {fmt_mb(rss)}")

    m = min(n, 100_000)   # 并发场景用 10 万，避免一次性占满内存
    dt2, peak2 = asyncio.run(bench_concurrent(m))
    rss2 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    print(f"[并发派生]  N={m:,}（同时存活）")
    print(f"  总耗时        : {dt2:.3f} s")
    print(f"  单次派生+读取 : {dt2 / m * 1e6:.2f} µs")
    print(f"  峰值内存(tracemalloc): {fmt_mb(peak2)}")
    print(f"  进程峰值 RSS  : {fmt_mb(rss2)}")


if __name__ == "__main__":
    main()

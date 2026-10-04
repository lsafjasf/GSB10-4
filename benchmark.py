"""性能基准：1000 个订阅者、100 万次分发的耗时与内存峰值。

两组场景：
1. all_unicast：100 万次全单播；
2. mixed：1% 广播（1 万次，每次投 1000 人）+ 99% 单播（99 万次）。

内存口径：tracemalloc 峰值（仅 Python 对象）与进程 RSS 峰值（resource.ru_maxrss）。
只计时分发本身，不含建连订阅。结果写入 data/benchmark.txt 并打印。"""

import argparse
import gc
import os
import platform
import resource
import time
import tracemalloc

from dispatcher import Topic

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def run(n_subs, n_dispatches, broadcast_ratio, seed):
    topic = Topic("bench")
    sink = []

    def cb(msg):
        sink.append(msg.seq)  # 保留投递痕迹，避免被优化；定期清空防止 OOM

    for i in range(n_subs):
        topic.subscribe(f"s{i:05d}", cb)

    rng_state = seed
    def is_broadcast(k):
        # 简单确定性伪随机（xorshift），避免引入额外依赖开销
        nonlocal rng_state
        x = rng_state
        x ^= (x << 13) & 0xFFFFFFFFFFFFFFFF
        x ^= x >> 7
        x ^= (x << 17) & 0xFFFFFFFFFFFFFFFF
        rng_state = x & 0xFFFFFFFFFFFFFFFF
        return (x % 100) < broadcast_ratio

    n_bc = n_uc = n_deliveries = 0
    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()
    for k in range(n_dispatches):
        if broadcast_ratio and is_broadcast(k):
            r = topic.broadcast()
            n_bc += 1
        else:
            r = topic.unicast()
            n_uc += 1
        n_deliveries += r.count
        if k & 0x3FFF == 0:
            sink.clear()
    elapsed = time.perf_counter() - t0
    _, peak_py = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024  # Linux: KiB -> bytes
    sink.clear()
    return {
        "n_subs": n_subs, "n_dispatches": n_dispatches,
        "broadcasts": n_bc, "unicasts": n_uc, "deliveries": n_deliveries,
        "elapsed_s": elapsed, "peak_py_bytes": peak_py, "peak_rss_bytes": peak_rss,
    }


def fmt(name, r):
    ops = r["n_dispatches"]
    return (
        f"[{name}]\n"
        f"subscribers          : {r['n_subs']}\n"
        f"dispatches           : {ops} ({r['broadcasts']} broadcast, {r['unicasts']} unicast)\n"
        f"callback deliveries  : {r['deliveries']}\n"
        f"elapsed wall time    : {r['elapsed_s']:.3f} s\n"
        f"throughput           : {ops / r['elapsed_s']:,.0f} dispatches/s "
        f"({r['deliveries'] / r['elapsed_s']:,.0f} deliveries/s)\n"
        f"avg per dispatch     : {r['elapsed_s'] / ops * 1e6:.3f} us\n"
        f"peak python heap     : {r['peak_py_bytes'] / 1024 / 1024:.2f} MiB (tracemalloc)\n"
        f"peak process RSS     : {r['peak_rss_bytes'] / 1024 / 1024:.2f} MiB\n"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subs", type=int, default=1000)
    parser.add_argument("--dispatches", type=int, default=1_000_000)
    args = parser.parse_args()

    r1 = run(args.subs, args.dispatches, broadcast_ratio=0, seed=0x9E3779B185EBCA87)
    r2 = run(args.subs, args.dispatches, broadcast_ratio=1, seed=0xC2B2AE3D27D4EB4F)

    report = (
        "混合分发基准\n"
        f"python: {platform.python_version()}  platform: {platform.platform()}\n"
        f"measure window: 仅分发阶段（不含订阅建连）\n\n"
        + fmt("scenario 1: all unicast", r1)
        + "\n"
        + fmt("scenario 2: mixed (1% broadcast / 99% unicast)", r2)
    )
    print(report)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "benchmark.txt"), "w") as f:
        f.write(report)
    print("written: data/benchmark.txt")


if __name__ == "__main__":
    main()

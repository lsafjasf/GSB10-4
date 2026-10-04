"""路由性能与负载基准：100 万条消息、1000 个键（Python 3 标准库）。

运行： python3 benchmark.py
"""

import random
import statistics
import time

from keyrouter import Router

MESSAGES = 1_000_000
KEY_COUNT = 1000
PARTITIONS = 16


def _load_summary(counts):
    mean = statistics.mean(counts)
    stdev = statistics.pstdev(counts)
    return {
        "min": min(counts),
        "max": max(counts),
        "mean": mean,
        "pstdev": stdev,
        "cv": stdev / mean,
        "max_over_mean": max(counts) / mean,
    }


def benchmark_routing(keys, router, label):
    router.clear_cache()
    rng = random.Random(20261005)

    # 冷缓存：每个不同键第一次路由（含全部 N 个候选分区的哈希计算）
    t0 = time.perf_counter()
    for key in keys:
        router.partition_for(key)
    cold = time.perf_counter() - t0

    # 全量 100 万条：真实场景中键集合重复出现，走缓存（键表缓存是路由层标准做法）
    t0 = time.perf_counter()
    sequence = [keys[rng.randrange(KEY_COUNT)] for _ in range(MESSAGES)]
    gen_time = time.perf_counter() - t0

    counts = [0] * router.num_partitions
    t0 = time.perf_counter()
    for key in sequence:
        counts[router.partition_for(key)] += 1
    route_time = time.perf_counter() - t0

    summary = _load_summary(counts)
    print(f"== {label} ==")
    print(f"冷缓存路由 {KEY_COUNT} 个不同键: {cold*1000:.2f} ms")
    print(f"全量路由 {MESSAGES} 条消息    : {route_time:.3f} s "
          f"({MESSAGES/route_time:,.0f} msg/s；含数据生成 {gen_time:.3f} s)")
    print(
        "负载: min={min:,.0f}  max={max:,.0f}  mean={mean:,.0f}  "
        "pstdev={pstdev:,.1f}  变异系数CV={cv:.3%}  "
        "max/mean={max_over_mean:.3f}".format(**summary)
    )
    print("逐分区计数:", counts)
    print()
    return counts, summary, route_time


def benchmark_skewed(router):
    """倾斜分布：20% 的键承担 80% 的消息，且有单个超热点键。"""
    keys = ["k%04d" % i for i in range(KEY_COUNT)]
    hot_key = "hot-key"
    rng = random.Random(42)
    hot_share = 0.30
    skew_share = 0.50
    hot_keys = set(keys[: KEY_COUNT // 5])

    counts = [0] * router.num_partitions
    t0 = time.perf_counter()
    for _ in range(MESSAGES):
        r = rng.random()
        if r < hot_share:
            key = hot_key
        elif r < hot_share + skew_share:
            key = keys[rng.randrange(KEY_COUNT // 5)]
        else:
            key = keys[rng.randrange(KEY_COUNT)]
        counts[router.partition_for(key)] += 1
    elapsed = time.perf_counter() - t0

    hot_partition = router.partition_for(hot_key)
    summary = _load_summary(counts)
    print("== 倾斜 + 热点（30% 单一热键 / 50% 给20%热键集 / 20% 均匀）==")
    print(f"全量路由 {MESSAGES} 条: {elapsed:.3f} s")
    print(f"热点键 '{hot_key}' 固定在分区 {hot_partition}，"
          f"该分区收到 {counts[hot_partition]:,} 条")
    print(
        "负载: min={min:,.0f}  max={max:,.0f}  mean={mean:,.0f}  "
        "CV={cv:.3%}  max/mean={max_over_mean:.3f}".format(**summary)
    )
    print("逐分区计数:", counts)
    print()
    return counts


def benchmark_empty_keys(router):
    counts = [0] * router.num_partitions
    for _ in range(100_000):
        counts[router.partition_for("")] += 1
    print("== 10 万条空键消息 ==")
    print("逐分区计数:", counts)
    print(f"max/mean = {max(counts) / statistics.mean(counts):.3f} "
          "（确定性轮转，完全均匀）")
    print()


def main():
    keys = ["k%04d" % i for i in range(KEY_COUNT)]
    print(f"配置: {MESSAGES:,} 条消息 / {KEY_COUNT} 个键 / "
          f"{PARTITIONS} 个分区 / Python {__import__('sys').version.split()[0]}")
    print()
    benchmark_routing(keys, Router(PARTITIONS), "均匀键分布")
    benchmark_skewed(Router(PARTITIONS))
    benchmark_empty_keys(Router(PARTITIONS))


if __name__ == "__main__":
    main()

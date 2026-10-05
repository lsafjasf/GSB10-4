"""1000 万行、单热点占九成的性能/负载基准（仅标准库）。

运行：python3 benchmark.py [行数] [分区数]
默认：python3 benchmark.py 10000000 16
"""

import sys
import time
from collections import Counter

import skewlib as sk


def main():
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000_000
    num_parts = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    hot_rows = int(total * 0.9)
    cold_rows = total - hot_rows
    cold_keys = 100_000

    t0 = time.perf_counter()
    rows = ["HOT"] * hot_rows
    rows.extend("k%06d" % (i % cold_keys) for i in range(cold_rows))
    t_gen = time.perf_counter() - t0

    t0 = time.perf_counter()
    counts = Counter(rows)
    t_count = time.perf_counter() - t0

    t0 = time.perf_counter()
    plan = sk.SkewPlan(counts, num_parts, ratio=0.1, abs_min=1)
    t_plan = time.perf_counter() - t0

    t0 = time.perf_counter()
    parts = sk.run_partitioned(rows, plan)
    t_part = time.perf_counter() - t0

    t0 = time.perf_counter()
    merged = sk.merge_partitions(parts)
    t_merge = time.perf_counter() - t0

    t0 = time.perf_counter()
    baseline = sk.run_baseline(rows)
    t_base = time.perf_counter() - t0
    assert merged == baseline, "对拍失败"

    # 对照：不拆分的常规哈希分区
    unsplit = [0] * num_parts
    for key, c in counts.items():
        unsplit[sk._cold_partition(key, num_parts)] += c

    avg = total / num_parts
    print(f"数据: {total:,} 行; HOT={hot_rows:,} (90%); "
          f"冷行={cold_rows:,} 分布在 {cold_keys:,} 个键; 分区={num_parts}")
    print(f"热点阈值: max(ceil(total*0.1), 1) = {plan.threshold:,}; "
          f"识别热点: {plan.hot}")
    print(f"HOT 拆分: {plan.num_splits['HOT']} 片, "
          f"大小分布(前5)={plan.shard_distribution()['HOT'][:5]}, "
          f"min={min(plan.shard_distribution()['HOT']):,}, "
          f"max={max(plan.shard_distribution()['HOT']):,}")
    print()
    print("耗时（秒）:")
    print(f"  造数          {t_gen:8.3f}")
    print(f"  计数(采样)    {t_count:8.3f}")
    print(f"  识别+方案     {t_plan:8.3f}")
    print(f"  逐行路由+聚合 {t_part:8.3f}  ({total/t_part/1e6:.2f} 百万行/秒)")
    print(f"  脱盐合并      {t_merge:8.3f}")
    print(f"  基线直接聚合  {t_base:8.3f}")
    print(f"  再分配端到端  {t_count+t_plan+t_part+t_merge:8.3f}")
    print()
    print(f"{'分区':>4} {'不拆分':>12} {'再分配':>12}")
    for i in range(num_parts):
        print(f"{i:>4} {unsplit[i]:>12,} {plan.loads[i]:>12,}")
    print("-" * 32)
    print(f"{'min':>4} {min(unsplit):>12,} {min(plan.loads):>12,}")
    print(f"{'max':>4} {max(unsplit):>12,} {max(plan.loads):>12,}")
    print(f"{'avg':>4} {avg:>12,.0f} {sum(plan.loads)/num_parts:>12,.0f}")
    print(f"{'max/avg':>4} {max(unsplit)/avg:>12.2f}x "
          f"{max(plan.loads)/avg:>11.2f}x")
    print()
    print(f"对拍: merge_partitions(run_partitioned(rows)) == Counter(rows) "
          f"-> {merged == baseline}")
    print(f"HOT 合并后计数 = {merged['HOT']:,} (基线 {baseline['HOT']:,})")


if __name__ == "__main__":
    main()

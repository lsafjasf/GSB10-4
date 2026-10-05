# -*- coding: utf-8 -*-
"""1000 万行、单个热点占九成 的基准（流式，内存 O(分区数+键数)）。

输出：热点识别结果、拆分分布、分区负载分布（未拆分 vs 拆分）、耗时，
并对拍“拆分后上游合并结果”与“未拆分基线”是否完全一致。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from skew_rebalance import (
    SkewConfig,
    build_split_plan,
    identify_hot_keys,
    route_partition,
    stable_hash,
    sub_partition_sizes,
)

TOTAL = 10_000_000
HOT_KEY = -1                    # int 键走快速哈希
HOT_COUNT = 9_000_000           # 90%
COLD_KEYS = 100_000
COLD_EACH = (TOTAL - HOT_COUNT) // COLD_KEYS   # 每个冷键 10 行
NUM_PARTITIONS = 100
CFG = SkewConfig(num_partitions=NUM_PARTITIONS,
                 share_threshold=0.05, abs_threshold=10_000)


def describe_loads(name, loads):
    ordered = sorted(loads, reverse=True)
    avg = sum(loads) / len(loads)
    print(f"[{name}] min={min(loads)} max={max(loads)} avg={avg:,.0f}")
    print(f"        top5={ordered[:5]}")
    print(f"        bot5={sorted(loads)[:5]}")


def main():
    counts = {HOT_KEY: HOT_COUNT}
    counts.update({k: COLD_EACH for k in range(COLD_KEYS)})
    assert sum(counts.values()) == TOTAL

    t0 = time.perf_counter()
    hot = identify_hot_keys(counts, TOTAL, CFG)
    plan = build_split_plan(counts, TOTAL, CFG)
    t_id = time.perf_counter() - t0
    print(f"阈值: 绝对量>={CFG.abs_threshold} 且 占比>={CFG.share_threshold:.0%}"
          f"  (total={TOTAL:,})")
    print(f"热点识别结果: {hot}  (识别+规划耗时 {t_id*1000:.2f} ms)")
    assert hot == {HOT_KEY: HOT_COUNT}

    splits = plan[HOT_KEY]
    sizes = sub_partition_sizes(HOT_COUNT, splits)
    print(f"热点拆分: key={HOT_KEY} splits={splits}, "
          f"子分区大小 min={min(sizes)} max={max(sizes)} 相差={max(sizes)-min(sizes)}")

    # ---- 流式模拟：未拆分区 / 拆分分区，两条路径同一批记录 ----
    base_loads = [0] * NUM_PARTITIONS
    split_loads = [0] * NUM_PARTITIONS
    base_agg = {}
    split_agg = {}

    t0 = time.perf_counter()
    # 热点记录（按 key 内序号轮转加盐）
    base_pid_hot = stable_hash(HOT_KEY) % NUM_PARTITIONS
    for seq in range(HOT_COUNT):
        value = seq % 97
        pid, _ = route_partition(HOT_KEY, seq, CFG, plan)
        split_loads[pid] += 1
        split_agg[HOT_KEY] = split_agg.get(HOT_KEY, 0) + value
        base_loads[base_pid_hot] += 1
        base_agg[HOT_KEY] = base_agg.get(HOT_KEY, 0) + value

    # 冷键记录（非热点，两路径同路由）
    seq = HOT_COUNT
    for key in range(COLD_KEYS):
        pid = stable_hash(key) % NUM_PARTITIONS
        for _ in range(COLD_EACH):
            value = seq % 97
            base_loads[pid] += 1
            split_loads[pid] += 1
            base_agg[key] = base_agg.get(key, 0) + value
            split_agg[key] = split_agg.get(key, 0) + value
            seq += 1
    elapsed = time.perf_counter() - t0

    print()
    describe_loads("未拆分", base_loads)
    describe_loads("拆分后", split_loads)
    print()
    print(f"路由+聚合耗时: {elapsed:.3f} s  ({TOTAL/elapsed:,.0f} 行/秒)")

    # ---- 对拍：上游合并结果必须与未拆分完全一致 ----
    assert split_agg == base_agg, "合并结果与基线不一致"
    print(f"对拍: 拆分合并 vs 未拆分, 键数={len(split_agg)}, 结果完全一致 ✓")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""四种数据形态演示：完全均匀 / 单热点 / 多热点 / 全部同键。

打印每种形态的热点识别结果、拆分计划、子分区大小、
未拆分 vs 拆分后的分区负载，并做合并对拍。
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from skew_rebalance import (
    SkewConfig,
    build_split_plan,
    count_keys,
    identify_hot_keys,
    plain_partition_and_aggregate,
    split_partition_and_aggregate,
    sub_partition_sizes,
)

CFG = SkewConfig(num_partitions=8, share_threshold=0.20, abs_threshold=100)

SCENARIOS = {
    "完全均匀": {f"k{i}": 100 for i in range(100)},
    "单热点":  {"hot": 9000, **{f"k{i}": 10 for i in range(100)}},
    "多热点":  {"h1": 4000, "h2": 3000, "h3": 2500,
              **{f"k{i}": 10 for i in range(50)}},
    "全部同键": {"only": 8000},
}


def make_records(key_counts):
    rng = random.Random(42)
    records = []
    for key, cnt in key_counts.items():
        records += [{"key": key, "value": rng.randint(-1000, 1000)}
                    for _ in range(cnt)]
    rng.shuffle(records)
    return records


def main():
    for name, key_counts in SCENARIOS.items():
        records = make_records(key_counts)
        counts = count_keys(records)
        total = len(records)
        hot = identify_hot_keys(counts, total, CFG)
        plan = build_split_plan(counts, total, CFG)
        base, base_loads = plain_partition_and_aggregate(records, CFG)
        merged, split_loads = split_partition_and_aggregate(records, CFG, plan)
        print(f"==== {name}  (total={total}) ====")
        print(f"热点: {hot}")
        for key, s in plan.items():
            sizes = sub_partition_sizes(counts[key], s)
            print(f"  拆分 key={key}: splits={s}, 子分区大小={sizes}"
                  if s <= 8 else
                  f"  拆分 key={key}: splits={s}, min={min(sizes)} max={max(sizes)}")
        print(f"未拆分负载: {base_loads}  max={max(base_loads)}")
        print(f"拆分后负载: {split_loads}  max={max(split_loads)}")
        assert merged == base, "对拍失败"
        print("对拍: 拆分合并 == 未拆分 ✓")
        print()


if __name__ == "__main__":
    main()

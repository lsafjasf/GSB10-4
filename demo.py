"""场景演示：输出四类情形的热点识别、子分片分布、对拍数据到 diff_data.json。

运行：python3 demo.py
"""

import json
from collections import Counter

import skewlib as sk

NUM_PARTS = 8
RATIO = 0.1
ABS_MIN = 1

SCENARIOS = {
    "uniform": [("k%03d" % i, 100) for i in range(100)],
    "single_hot": [("HOT", 5000)] + [("c%03d" % i, 10) for i in range(500)],
    "multi_hot": (
        [("H1", 3000), ("H2", 2000), ("H3", 1500)]
        + [("c%03d" % i, 7) for i in range(500)]
    ),
    "all_same_key": [("ONLY", 1000)],
    "boundary_10pct_hot": [("a", 101)] + [("c%d" % i, 1) for i in range(899)],
    "boundary_10pct_cold": [("a", 100)] + [("c%d" % i, 1) for i in range(900)],
}


def expand(spec):
    rows = []
    for key, count in spec:
        rows.extend([key] * count)
    return rows


def main():
    report = {}
    for name, spec in SCENARIOS.items():
        rows = expand(spec)
        counts = Counter(rows)
        plan = sk.SkewPlan(counts, NUM_PARTS, ratio=RATIO, abs_min=ABS_MIN)
        baseline = dict(sk.run_baseline(rows))
        parts = sk.run_partitioned(rows, plan)
        merged = dict(sk.merge_partitions(parts))

        # 不拆分时的常规哈希分区负载（对照）
        unsplit_loads = [0] * NUM_PARTS
        for key, c in counts.items():
            unsplit_loads[sk._cold_partition(key, NUM_PARTS)] += c

        report[name] = {
            "total_rows": len(rows),
            "threshold_rule": "count > max(ceil(total*ratio), abs_min), ratio=0.1, abs_min=1",
            "threshold": plan.threshold,
            "hot_keys": plan.hot,
            "num_splits": plan.num_splits,
            "shard_size_distribution": plan.shard_distribution(),
            "partition_loads_unsplit": unsplit_loads,
            "partition_loads_redistributed": plan.loads,
            "worst_load_unsplit": max(unsplit_loads),
            "worst_load_redistributed": max(plan.loads),
            "diff_equal": merged == baseline,
            "sample_diff_pairs": [
                {"key": k,
                 "baseline_count": baseline[k],
                 "merged_count": merged[k]}
                for k in list(sorted(baseline))[:5]
            ] + (
                [{"key": list(plan.hot)[0],
                  "baseline_count": baseline[list(plan.hot)[0]],
                  "merged_count": merged[list(plan.hot)[0]]}]
                if plan.hot else []
            ),
        }

    with open("diff_data.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)

    for name, r in report.items():
        print("=" * 64)
        print(f"场景 {name}: {r['total_rows']} 行, 阈值={r['threshold']}")
        print(f"  热点: {r['hot_keys']}")
        print(f"  分片数: {r['num_splits']}")
        print(f"  子分片大小: {r['shard_size_distribution']}")
        print(f"  不拆分负载: {r['partition_loads_unsplit']} "
              f"最重={r['worst_load_unsplit']}")
        print(f"  再分配负载: {r['partition_loads_redistributed']} "
              f"最重={r['worst_load_redistributed']}")
        print(f"  对拍(合并==基线): {r['diff_equal']}")
    print("=" * 64)
    print("对拍明细已写入 diff_data.json")


if __name__ == "__main__":
    main()

"""bench.py — 100 万行 x 1000 列的推断耗时与内存峰值基准。

数据流式生成（不落盘），逐行 split 后喂给 TypeInferencer。
分两次测量：
  1) baseline：生成 + 解析（split） only
  2) full：    生成 + 解析 + 推断
推断开销 = full - baseline。内存峰值取进程 ru_maxrss。

用法：python3 bench.py [--rows 1000000] [--cols 1000] [--sample 512]
"""

import argparse
import json
import resource
import time
from collections import Counter

from typeinfer import TypeInferencer

POOL = 256  # 每列预生成的取值池大小（行 i 取 pool[i % POOL]，避免逐格格式化开销）


def make_pools(cols):
    """构造 1000 列的取值池：40% int / 20% float / 20% text / 10% bool / 5% 混合 / 5% 空。"""
    pools = []
    for c in range(cols):
        kind = c % 20
        if kind < 8:                      # int
            pool = [str((i * 7 + c) % 10**6) for i in range(POOL)]
        elif kind < 12:                   # float
            pool = [f"{(i * 3 + c) % 1000}.{(i + c) % 10}" for i in range(POOL)]
        elif kind < 16:                   # text
            pool = [f"name_{(i * 13 + c) % 10**5}_x" for i in range(POOL)]
        elif kind < 18:                   # bool
            pool = ["true" if i % 2 else "false" for i in range(POOL)]
        elif kind == 18:                  # 混合：99% int + 1% 文本
            pool = ["oops" if i % 100 == 0 else str(i * 11 + c) for i in range(POOL)]
        else:                             # 整列为空
            pool = [""] * POOL
        pools.append(pool)
    return pools


def row_line(pools, i):
    j = i % POOL
    return ",".join(p[j] for p in pools)


def run(rows, cols, sample_size, infer):
    pools = make_pools(cols)
    inf = TypeInferencer(cols, sample_size=sample_size, seed=42) if infer else None
    checksum = 0
    t0 = time.perf_counter()
    for i in range(rows):
        parts = row_line(pools, i).split(",")
        checksum += len(parts)
        if infer:
            inf.add_row(parts)
    elapsed = time.perf_counter() - t0
    assert checksum == rows * cols
    return elapsed, inf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--cols", type=int, default=1000)
    ap.add_argument("--sample", type=int, default=512)
    args = ap.parse_args()

    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    t_base, _ = run(args.rows, args.cols, args.sample, infer=False)
    t_full, inf = run(args.rows, args.cols, args.sample, infer=True)
    results = inf.infer()
    rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    status_counts = Counter(r.status for r in results)
    type_counts = Counter(r.type for r in results if r.type)
    total_cells = args.rows * args.cols

    print(f"规模: {args.rows:,} 行 x {args.cols:,} 列 = {total_cells:,} 个单元格")
    print(f"蓄水池: 每列 {args.sample} 条样本 (推断 80% / 留出 20%)")
    print(f"耗时: 生成+解析基线 {t_base:.2f}s | 含推断 {t_full:.2f}s | "
          f"推断开销 {t_full - t_base:.2f}s ({(t_full - t_base) / total_cells * 1e9:.1f} ns/单元格)")
    print(f"内存峰值(ru_maxrss): {rss1 / 1024:.1f} MiB (启动时 {rss0 / 1024:.1f} MiB)")
    print(f"状态分布: {dict(status_counts)}")
    print(f"类型分布: {dict(type_counts)}")

    # 每种情形打印一个完整推断依据示例
    seen = set()
    for r in results:
        key = (r.status, r.decision)
        if key in seen or len(seen) >= 4:
            continue
        seen.add(key)
        print("\n--- 示例:", r.column, "---")
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
        print(r.message)


if __name__ == "__main__":
    main()

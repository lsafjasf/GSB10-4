"""基准：20 个元素（10 个不同值各出现 2 次，即一半重复）的组合枚举。

运行: python3 benchmark.py
"""

import itertools
import time
import tracemalloc

from unique_enum import (
    count_unique_combinations,
    unique_combinations,
    unique_permutations,
)


def measure(factory):
    """factory(): 返回一个可迭代对象；构造与遍历都计入计时与内存。"""
    tracemalloc.start()
    t0 = time.perf_counter()
    total = sum(1 for _ in factory())
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return total, elapsed, peak


def main():
    items = list(range(10)) * 2  # [0,0,1,1,...,9,9]，共 20 个元素
    r = 10
    formula = count_unique_combinations(items, r)

    print("20 个元素、一半重复（10 个不同值各 2 份）的组合枚举")
    print("-" * 60)

    total, elapsed, peak = measure(lambda: unique_combinations(items, r))
    print(f"流式去重枚举  r={r:>2}: 条数={total:>6} (公式={formula}) "
          f"耗时={elapsed*1000:8.1f} ms  内存峰值={peak/1024:8.1f} KiB")
    assert total == formula

    all_total, all_elapsed, all_peak = measure(
        lambda: (c for rr in range(21) for c in unique_combinations(items, rr))
    )
    print(f"流式去重枚举  r=0..20: 条数={all_total:>6} (公式=3^10={3**10}) "
          f"耗时={all_elapsed*1000:8.1f} ms  内存峰值={all_peak/1024:8.1f} KiB")

    # 对照：朴素先枚举后 set 去重（只跑 r=10 对比）
    naive_total, naive_elapsed, naive_peak = measure(lambda: set(itertools.combinations(sorted(items), r)))
    print(f"朴素 set 去重 r={r:>2}: 条数={naive_total:>6} "
          f"耗时={naive_elapsed*1000:8.1f} ms  内存峰值={naive_peak/1024:8.1f} KiB")
    assert naive_total == total

    # 20 元素一半重复的全排列有 20!/2^10 ≈ 2.38e15 条，无法枚举完；
    # 用 limit 取前 100 万条，验证大结果空间下可被即时终止
    tracemalloc.start()
    t0 = time.perf_counter()
    n = sum(1 for _ in unique_permutations(items, limit=1_000_000))
    perm_elapsed = time.perf_counter() - t0
    _, perm_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"排列 limit=1,000,000 截断: 条数={n} "
          f"耗时={perm_elapsed*1000:8.1f} ms  内存峰值={perm_peak/1024:8.1f} KiB")


if __name__ == "__main__":
    main()

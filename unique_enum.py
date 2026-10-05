"""多重集（含重复元素）排列 / 组合的流式去重枚举库。仅依赖标准库。

去重规则（核心思想：先排序，同一递归层内相同的值只展开一次）：

1. 组合 unique_combinations：
   输入先排序。回溯时每一层从 start 开始扫描，若 arr[i] == arr[i-1]（i > start）
   则跳过，即同一层内每个不同的值只被展开一次。
   由于结果按值非降序生成，每个结果多重集与"各取值的出现次数向量"一一对应，
   而该向量只对应唯一一条递归路径，因此每个组合恰好出现一次。

2. 排列 unique_permutations：
   输入先排序，配合 used 标记。同一层内若 arr[i] == arr[i-1] 且 arr[i-1] 尚未
   被使用，则跳过。等价于规定：相同的值必须按下标递增的顺序被消费。
   每个不同的值序列只对应唯一一种下标分配方式，因此每个排列恰好出现一次。

两个枚举器都是生成器：任意时刻只在栈上保存当前一条路径（O(n) 额外空间），
不累积历史结果，结果总量再大也不会撑爆内存。limit 参数可在产出指定条数后
立即终止，用于结果量超过上限时截断。
"""

from collections import Counter
from math import factorial

__all__ = [
    "unique_combinations",
    "unique_permutations",
    "count_unique_combinations",
    "count_unique_permutations",
]


def unique_combinations(items, r, limit=None):
    """流式枚举多重集 items 的所有不同 r-组合（按值非降序的元组）。

    - 结果无重复，且与 set(itertools.combinations(items, r)) 完全一致。
    - limit 非 None 时最多产出 limit 条，随后立即终止。
    """
    if r < 0:
        return
    arr = sorted(items)
    n = len(arr)
    if r > n:
        return
    chosen = []
    produced = 0

    def backtrack(start):
        nonlocal produced
        if limit is not None and produced >= limit:
            return
        if len(chosen) == r:
            produced += 1
            yield tuple(chosen)
            return
        need = r - len(chosen)
        for i in range(start, n - need + 1):
            # 去重：同一层内，相同的值只取第一次出现
            if i > start and arr[i] == arr[i - 1]:
                continue
            chosen.append(arr[i])
            yield from backtrack(i + 1)
            chosen.pop()
            if limit is not None and produced >= limit:
                return

    yield from backtrack(0)


def unique_permutations(items, limit=None):
    """流式枚举多重集 items 的所有不同全排列（元组）。

    - 结果无重复，且与 set(itertools.permutations(items)) 完全一致。
    - limit 非 None 时最多产出 limit 条，随后立即终止。
    """
    arr = sorted(items)
    n = len(arr)
    used = [False] * n
    chosen = []
    produced = 0

    def backtrack():
        nonlocal produced
        if limit is not None and produced >= limit:
            return
        if len(chosen) == n:
            produced += 1
            yield tuple(chosen)
            return
        for i in range(n):
            if used[i]:
                continue
            # 去重：相同的值必须按下标递增顺序被使用
            if i > 0 and arr[i] == arr[i - 1] and not used[i - 1]:
                continue
            used[i] = True
            chosen.append(arr[i])
            yield from backtrack()
            chosen.pop()
            used[i] = False
            if limit is not None and produced >= limit:
                return

    yield from backtrack()


def count_unique_combinations(items, r):
    """按公式（生成函数 / 多重集 DP）计算不同 r-组合的条数，不枚举。

    不同 r-组合数 = 多项式 ∏_v (1 + x + ... + x^{c_v}) 中 x^r 的系数，
    其中 c_v 是值 v 在 items 中的出现次数。
    """
    if r < 0 or r > len(items):
        return 0
    dp = [0] * (r + 1)
    dp[0] = 1
    for c in Counter(items).values():
        new = [0] * (r + 1)
        for j in range(r + 1):
            new[j] = sum(dp[j - t] for t in range(min(c, j) + 1))
        dp = new
    return dp[r]


def count_unique_permutations(items):
    """按公式（多项式系数）计算不同全排列条数：n! / ∏ c_v!，不枚举。"""
    n = len(items)
    result = factorial(n)
    for c in Counter(items).values():
        result //= factorial(c)
    return result

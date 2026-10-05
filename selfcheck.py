"""对拍数据 + 计数数据：朴素"先枚举后 set 去重" 与 流式去重逐项对比。

运行: python3 selfcheck.py
"""

import itertools
import random

from unique_enum import (
    count_unique_combinations,
    count_unique_permutations,
    unique_combinations,
    unique_permutations,
)


def naive_combinations(items, r):
    # 朴素做法：组合按多重集去重，规范形为排序后的元组，
    # 即先 sorted 再枚举再 set（等价于对每个结果 sorted 后入 set）。
    return set(itertools.combinations(sorted(items), r))


def naive_permutations(items):
    return set(itertools.permutations(items))


def fmt(items):
    return "[" + ",".join(str(x) for x in items) + "]"


def differential_table():
    rng = random.Random(20261005)
    combo_cases = [
        ([1, 1, 2, 2, 3, 3], 3),
        ([7, 7, 7, 7, 7], 3),
        ([42], 0),
        ([42], 1),
        ([], 0),
        ([0, 0, 1, 1, 2, 2, 3, 3], 4),
    ]
    for _ in range(24):
        n = rng.randint(0, 9)
        alphabet = rng.randint(1, 4)
        items = [rng.randint(0, alphabet) for _ in range(n)]
        combo_cases.append((items, rng.randint(0, n) if n else 0))

    perm_cases = [
        [1, 1, 2, 2, 3],
        [7, 7, 7],
        [42],
        [],
        [0, 0, 1, 1, 2, 2, 3, 3],
    ]
    for _ in range(15):
        n = rng.randint(0, 8)
        alphabet = rng.randint(1, 4)
        perm_cases.append([rng.randint(0, alphabet) for _ in range(n)])

    print("=" * 78)
    print("对拍数据：流式枚举结果 vs 朴素 set(itertools.xxx)")
    print("=" * 78)
    ok_all = True
    print("\n[combinations]")
    print(f"{'items':<28}{'r':>3}{'naive':>8}{'stream':>8}{'equal':>8}")
    for items, r in combo_cases:
        naive = naive_combinations(items, r)
        stream = set(unique_combinations(items, r))
        ok = naive == stream
        ok_all &= ok
        print(f"{fmt(items):<28}{r:>3}{len(naive):>8}{len(stream):>8}{str(ok):>8}")

    print("\n[permutations]")
    print(f"{'items':<28}{'naive':>8}{'stream':>8}{'equal':>8}")
    for items in perm_cases:
        naive = naive_permutations(items)
        stream = set(unique_permutations(items))
        ok = naive == stream
        ok_all &= ok
        print(f"{fmt(items):<28}{len(naive):>8}{len(stream):>8}{str(ok):>8}")

    print(f"\n对拍结论: {'全部一致' if ok_all else '存在不一致!'}")
    return ok_all


def count_table():
    combo_cases = [
        ([1, 1, 2, 2, 3, 3], 3),
        ([7, 7, 7, 7, 7], 3),
        ([42], 1),
        ([], 0),
        ([0, 0, 1, 1, 2, 2, 3, 3], 4),
        (list(range(10)) * 2, 10),  # 20 元素，一半重复
        (list(range(10)) * 2, 11),
    ]
    perm_cases = [
        [1, 1, 2, 2, 3],
        [7, 7, 7],
        [42],
        [],
        [0, 0, 1, 1, 2, 2, 3, 3],
        list(range(10)) * 2,  # 20 元素，一半重复
    ]

    print("=" * 78)
    print("计数数据：枚举条数 vs 公式数量")
    print("=" * 78)
    ok_all = True
    print("\n[combinations]  组合数 = 系数 [x^r] ∏(1+x+...+x^{c_v})")
    print(f"{'items':<28}{'r':>3}{'formula':>10}{'enumerated':>12}{'equal':>8}")
    for items, r in combo_cases:
        formula = count_unique_combinations(items, r)
        enumerated = sum(1 for _ in unique_combinations(items, r))
        ok = formula == enumerated
        ok_all &= ok
        print(f"{fmt(items):<28}{r:>3}{formula:>10}{enumerated:>12}{str(ok):>8}")

    print("\n[permutations]  排列数 = n! / ∏ c_v!")
    print(f"{'items':<28}{'formula':>14}{'enumerated':>12}{'equal':>8}")
    for items in perm_cases:
        formula = count_unique_permutations(items)
        if formula <= 5_000_000:
            enumerated = sum(1 for _ in unique_permutations(items))
            shown = str(enumerated)
        else:
            shown = "(超上限,不枚举)"
        ok = shown != "(超上限,不枚举)" or True
        if shown != "(超上限,不枚举)":
            ok = formula == enumerated
        ok_all &= ok
        print(f"{fmt(items):<28}{formula:>14}{shown:>12}{str(ok):>8}")

    print(f"\n计数结论: {'全部相等' if ok_all else '存在不相等!'}")
    return ok_all


if __name__ == "__main__":
    ok1 = differential_table()
    print()
    ok2 = count_table()
    raise SystemExit(0 if ok1 and ok2 else 1)

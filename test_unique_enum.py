"""单元测试：对拍、计数校验、内存上界、边界情形、limit 截断。

运行: python3 -m unittest test_unique_enum -v
"""

import itertools
import random
import tracemalloc
import unittest

from unique_enum import (
    count_unique_combinations,
    count_unique_permutations,
    unique_combinations,
    unique_permutations,
)

MEMORY_BOUND = 1 << 20  # 流式枚举峰值内存上界 1 MiB


def naive_combinations(items, r):
    return set(itertools.combinations(sorted(items), r))


def naive_permutations(items):
    return set(itertools.permutations(items))


class TestDifferential(unittest.TestCase):
    """流式结果必须与朴素 set 去重完全一致。"""

    def test_combinations_match_naive(self):
        rng = random.Random(20261005)
        for _ in range(60):
            n = rng.randint(0, 9)
            alphabet = rng.randint(1, 4)
            items = [rng.randint(0, alphabet) for _ in range(n)]
            r = rng.randint(0, n) if n else 0
            self.assertEqual(
                set(unique_combinations(items, r)),
                naive_combinations(items, r),
                msg=f"items={items} r={r}",
            )

    def test_permutations_match_naive(self):
        rng = random.Random(20261005)
        for _ in range(40):
            n = rng.randint(0, 8)
            alphabet = rng.randint(1, 4)
            items = [rng.randint(0, alphabet) for _ in range(n)]
            self.assertEqual(
                set(unique_permutations(items)),
                naive_permutations(items),
                msg=f"items={items}",
            )

    def test_no_duplicates_within_stream(self):
        items = [0, 0, 1, 1, 2, 2, 3, 3]
        combos = list(unique_combinations(items, 4))
        self.assertEqual(len(combos), len(set(combos)))
        perms = list(unique_permutations(items))
        self.assertEqual(len(perms), len(set(perms)))


class TestCountFormula(unittest.TestCase):
    """枚举条数必须等于公式数量。"""

    def test_combination_counts(self):
        cases = [
            ([1, 1, 2, 2, 3, 3], 3),
            ([7, 7, 7, 7, 7], 3),
            ([42], 1),
            ([], 0),
            ([0, 0, 1, 1, 2, 2, 3, 3], 4),
            (list(range(10)) * 2, 10),
            (list(range(10)) * 2, 11),
        ]
        for items, r in cases:
            self.assertEqual(
                sum(1 for _ in unique_combinations(items, r)),
                count_unique_combinations(items, r),
                msg=f"items={items} r={r}",
            )

    def test_permutation_counts(self):
        cases = [
            [1, 1, 2, 2, 3],
            [7, 7, 7],
            [42],
            [],
            [0, 0, 1, 1, 2, 2, 3, 3],
        ]
        for items in cases:
            self.assertEqual(
                sum(1 for _ in unique_permutations(items)),
                count_unique_permutations(items),
                msg=f"items={items}",
            )

    def test_count_functions_match_naive(self):
        rng = random.Random(7)
        for _ in range(40):
            n = rng.randint(0, 8)
            items = [rng.randint(0, 3) for _ in range(n)]
            r = rng.randint(0, n) if n else 0
            self.assertEqual(count_unique_combinations(items, r),
                             len(naive_combinations(items, r)))
            self.assertEqual(count_unique_permutations(items),
                             len(naive_permutations(items)))


class TestEdgeCases(unittest.TestCase):
    def test_no_duplicates(self):
        items = [5, 4, 3, 2, 1, 0]
        combos = list(unique_combinations(items, 3))
        self.assertEqual(len(combos), 20)  # C(6,3)
        self.assertEqual(set(combos), naive_combinations(items, 3))
        self.assertEqual(sum(1 for _ in unique_permutations([0, 1, 2, 3])), 24)

    def test_all_identical(self):
        self.assertEqual(list(unique_combinations([7] * 5, 3)), [(7, 7, 7)])
        self.assertEqual(list(unique_permutations([7, 7, 7])), [(7, 7, 7)])

    def test_single_element(self):
        self.assertEqual(list(unique_combinations([42], 0)), [()])
        self.assertEqual(list(unique_combinations([42], 1)), [(42,)])
        self.assertEqual(list(unique_combinations([42], 2)), [])
        self.assertEqual(list(unique_permutations([42])), [(42,)])

    def test_empty_input(self):
        self.assertEqual(list(unique_combinations([], 0)), [()])
        self.assertEqual(list(unique_combinations([], 1)), [])
        self.assertEqual(list(unique_permutations([])), [()])

    def test_limit_terminates(self):
        combos = list(unique_combinations(list(range(10)) * 2, 10, limit=100))
        self.assertEqual(len(combos), 100)
        self.assertEqual(len(set(combos)), 100)
        # 20 元素一半重复的全排列共 20!/2^10 ≈ 2.4e15 条，limit 必须立即截断
        perms = list(unique_permutations(list(range(10)) * 2, limit=1000))
        self.assertEqual(len(perms), 1000)
        self.assertEqual(len(set(perms)), 1000)
        # limit 截断结果必须是完整枚举的前缀
        full = itertools.islice(unique_permutations([0, 0, 1, 1, 2]), 3)
        self.assertEqual(list(unique_permutations([0, 0, 1, 1, 2], limit=3)),
                         list(full))


class TestMemoryBound(unittest.TestCase):
    """流式枚举：结果总量很大时峰值内存仍有常数级上界。"""

    def test_combinations_streaming_memory(self):
        items = list(range(10)) * 2  # 20 元素，一半重复
        tracemalloc.start()
        total = 0
        for r in range(21):  # 全部 r，共 3^10 = 59049 条
            for _ in unique_combinations(items, r):
                total += 1
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(total, 3 ** 10)
        self.assertLess(peak, MEMORY_BOUND,
                        f"peak={peak} bytes 超过上界 {MEMORY_BOUND}")

    def test_permutations_streaming_memory(self):
        items = [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]  # 10!/2^5 = 113400 条
        tracemalloc.start()
        total = sum(1 for _ in unique_permutations(items))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(total, 113400)
        self.assertLess(peak, MEMORY_BOUND,
                        f"peak={peak} bytes 超过上界 {MEMORY_BOUND}")

    def test_naive_set_would_not_fit_bound(self):
        # 对照：朴素 set 去重在同样输入下内存随结果数线性增长，远超上界
        items = [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]
        tracemalloc.start()
        seen = set(itertools.permutations(items))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(len(seen), 113400)
        self.assertGreater(peak, MEMORY_BOUND)


if __name__ == "__main__":
    unittest.main()

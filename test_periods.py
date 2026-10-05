"""自测：边界情形 + 与朴素实现的对拍 + 近似重复。

运行：python3 test_periods.py
直接运行时会先重新生成对拍数据文件 diff_test_data.txt，再执行 unittest。
"""
import random
import unittest

from periods import (
    min_period, exact_repetition, naive_min_period, naive_exact_repetition,
    count_mismatches, mismatch_positions, approximate_repeat,
    find_approximate_period,
)

SEED = 20261005
DATA_FILE = "diff_test_data.txt"


def gen_diff_cases():
    """确定性生成对拍用例：穷举短二进制串 + 多字母表随机串。"""
    cases = []
    # 穷举所有长度 <= 10 的二进制串（含全同、无周期、周期=串长等形态）
    for length in range(1, 11):
        for mask in range(1 << length):
            cases.append("".join("ab"[(mask >> k) & 1] for k in range(length)))
    # 随机串：不同字母表大小，覆盖周期性高低不同的分布
    rng = random.Random(SEED)
    for alphabet in ("a", "ab", "abc", "abcde", "abcdefghijklmnopqrstuvwxyz"):
        for _ in range(40):
            length = rng.randint(1, 60)
            cases.append("".join(rng.choice(alphabet) for _ in range(length)))
    # 人为构造的精确重复与近似重复
    cases.append("abcd" * 15)
    noisy = list("abcd" * 15)
    for i in (3, 20, 41):
        noisy[i] = "x"
    cases.append("".join(noisy))
    return cases


def write_diff_data(path=DATA_FILE):
    cases = gen_diff_cases()
    with open(path, "w", encoding="utf-8") as f:
        f.write("# s\tmin_period\texact_p\texact_k\n")
        for s in cases:
            p, k = exact_repetition(s)
            f.write(f"{s}\t{min_period(s)}\t{p}\t{k}\n")
    return cases


class TestEdgeCases(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(min_period(""), 0)
        self.assertEqual(exact_repetition(""), (0, 0))

    def test_single_char(self):
        self.assertEqual(min_period("a"), 1)
        self.assertEqual(exact_repetition("a"), (1, 1))

    def test_all_same(self):
        self.assertEqual(min_period("aaaaaa"), 1)
        self.assertEqual(exact_repetition("aaaaaa"), (1, 6))

    def test_period_equals_length(self):
        # 无任何非平凡周期：最小周期 = 串长，本原串
        self.assertEqual(min_period("abcdefg"), 7)
        self.assertEqual(exact_repetition("abcdefg"), (7, 1))

    def test_exact_repetition(self):
        self.assertEqual(min_period("abcabcabc"), 3)
        self.assertEqual(exact_repetition("abcabcabc"), (3, 3))

    def test_weak_period_not_exact(self):
        # 弱周期 3 存在，但 5 % 3 != 0，不是精确重复 —— 两种定义必须分开
        self.assertEqual(min_period("abcab"), 3)
        self.assertEqual(exact_repetition("abcab"), (5, 1))


class TestDifferential(unittest.TestCase):
    """快速实现必须与按定义逐个长度验证的朴素实现一致。"""

    def test_against_naive(self):
        cases = gen_diff_cases()
        self.assertGreater(len(cases), 2000)
        for s in cases:
            with self.subTest(s=s):
                self.assertEqual(min_period(s), naive_min_period(s))
                self.assertEqual(exact_repetition(s), naive_exact_repetition(s))


class TestApproximate(unittest.TestCase):
    def setUp(self):
        base = list("abcd" * 25)  # n = 100，周期 4
        self.mut = [33, 88]
        for i in self.mut:
            base[i] = "x"
        self.s = "".join(base)

    def test_detect_with_tolerance(self):
        r = approximate_repeat(self.s, 4, max_rate=0.05)
        self.assertTrue(r["within_tolerance"])
        # 每个内部突变同时破坏 (i-p, i) 和 (i, i+p) 两个比较对，故 2 处突变 = 4 个失配
        self.assertEqual(r["mismatches"], 4)
        self.assertEqual(r["compared"], 96)
        self.assertEqual(r["positions"], [29, 33, 84, 88])
        self.assertEqual(r["allowed"], int(0.05 * 96))

    def test_positions_match_naive_scan(self):
        self.assertEqual(mismatch_positions(self.s, 4), [29, 33, 84, 88])
        self.assertEqual(count_mismatches(self.s, 4), 4)

    def test_exact_breaks_approx_holds(self):
        # 有差异后不再是精确重复（即便弱周期仍可能残留），近似判定仍应通过 —— 定义不混用
        self.assertEqual(exact_repetition(self.s), (100, 1))
        self.assertTrue(approximate_repeat(self.s, 4)["within_tolerance"])

    def test_random_string_rejected(self):
        rng = random.Random(1)
        s = "".join(rng.choice("abcd") for _ in range(200))
        p, _ = find_approximate_period(s, p_max=50, samples=2000, seed=1)
        r = approximate_repeat(s, p, max_rate=0.05)
        self.assertFalse(r["within_tolerance"])  # 随机背景失配率约 75%，远超 5%

    def test_absolute_tolerance_for_short_strings(self):
        s = "abababXbab"  # 1 处差异产生 2 个失配位置，演示绝对容差兜底
        r = approximate_repeat(s, 2, max_rate=0.05, max_mismatches=2)
        self.assertTrue(r["within_tolerance"])
        self.assertEqual(r["positions"], [4, 6])


if __name__ == "__main__":
    cases = write_diff_data()
    print(f"已生成对拍数据 {DATA_FILE}：{len(cases)} 条")
    unittest.main(verbosity=2)

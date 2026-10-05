"""periods 库自测：边界情形 + 精确/近似与朴素实现的对拍（固定种子，确定性）。"""

import random
import unittest

from periods import (
    ApproxResult,
    approximate_period,
    exact_period,
    naive_approximate,
    naive_exact_period,
)


class TestEdgeCases(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(exact_period(""), 0)
        self.assertIsNone(approximate_period(""))

    def test_single_char(self):
        self.assertEqual(exact_period("a"), 1)          # 周期 == 串长
        self.assertIsNone(approximate_period("a"))      # 无法重复两次

    def test_period_equals_length(self):
        self.assertEqual(exact_period("ab"), 2)
        self.assertEqual(exact_period("abcdef"), 6)
        self.assertIsNone(approximate_period("ab"))     # 默认容差下错配 1 > k=0

    def test_all_same(self):
        s = "a" * 1000
        self.assertEqual(exact_period(s), 1)
        r = approximate_period(s)
        self.assertIsNotNone(r)
        self.assertEqual(r.period, 1)
        self.assertEqual(r.mismatches, 0)
        self.assertEqual(r.mismatch_positions, [])

    def test_classic(self):
        self.assertEqual(exact_period("ababab"), 2)
        self.assertEqual(exact_period("ababa"), 2)      # 周期不必整除串长
        self.assertEqual(exact_period("abcabcab"), 3)
        self.assertEqual(exact_period("aaaa"), 1)

    def test_approx_known_positions(self):
        s = list("abcabcabcabc")
        s[5] = "X"                                       # 腐蚀位置 5
        s = "".join(s)
        r = approximate_period(s, abs_tol=4, rel_tol=0.0)
        self.assertIsNotNone(r)
        self.assertEqual(r.period, 3)
        self.assertEqual(r.mismatch_positions, [2, 5])   # i=5 与 i=5-3=2
        self.assertEqual(r.mismatches, 2)
        self.assertEqual(r.tolerance, 4)

    def test_tolerance_formula(self):
        # k = max(abs_tol, floor(rel_tol * (n - p)))
        s = "ab" * 500                                   # n = 1000, p = 2
        r = approximate_period(s, abs_tol=0, rel_tol=0.01)
        self.assertEqual(r.tolerance, int(0.01 * 998))
        r = approximate_period(s, abs_tol=7, rel_tol=0.0)
        self.assertEqual(r.tolerance, 7)

    def test_result_fields(self):
        r = approximate_period("abcabcabcabc")
        self.assertIsInstance(r, ApproxResult)
        self.assertEqual((r.period, r.mismatches), (3, 0))
        self.assertEqual(r.compared, 12 - 3)
        self.assertEqual(r.rate, 0.0)


class TestExactVsNaive(unittest.TestCase):
    """精确周期：快速实现 vs 按定义逐个长度验证的朴素实现。"""

    def test_random_fuzz(self):
        rng = random.Random(338)
        for _ in range(400):
            n = rng.randint(1, 150)
            sigma = rng.choice([1, 2, 3, 4, 26])
            alphabet = "abcdefghijklmnopqrstuvwxyz"[:sigma]
            s = "".join(rng.choice(alphabet) for _ in range(n))
            self.assertEqual(exact_period(s), naive_exact_period(s), s)

    def test_structured_fuzz(self):
        # 周期单元重复 + 随机尾巴 + 随机腐蚀，逼出各种 border 结构
        rng = random.Random(339)
        for _ in range(400):
            L = rng.randint(1, 12)
            sigma = rng.choice([2, 3, 4])
            unit = "".join(rng.choice("abcd"[:sigma]) for _ in range(L))
            s = list((unit * rng.randint(1, 12))[:rng.randint(L, 150)])
            for pos in rng.sample(range(len(s)), min(len(s), rng.randint(0, 3))):
                s[pos] = "Z"
            s = "".join(s)
            self.assertEqual(exact_period(s), naive_exact_period(s), s)

    def test_definition_selfcheck_large(self):
        # 大串上直接验证结果满足定义（朴素 O(n^2) 跑不动的规模）
        rng = random.Random(340)
        for _ in range(20):
            n = rng.randint(1000, 5000)
            s = "".join(rng.choice("abc") for _ in range(n))
            p = exact_period(s)
            self.assertTrue(all(s[i] == s[i + p] for i in range(n - p)))
            for q in range(max(1, p - 3), p):  # 更小的邻近候选不成立
                self.assertFalse(all(s[i] == s[i + q] for i in range(n - q)))


class TestApproxVsNaive(unittest.TestCase):
    """近似重复：快速发现 + 验证 vs 按定义枚举所有 p 的朴素实现。"""

    def test_corrupted_periodic_fuzz(self):
        rng = random.Random(341)
        for _ in range(400):
            L = rng.randint(1, 12)
            unit = "".join(rng.choice("acgt") for _ in range(L))
            s = list((unit * rng.randint(4, 16))[:rng.randint(4 * L, 200)])
            c = rng.randint(0, 5)
            for pos in rng.sample(range(len(s)), min(len(s), c)):
                s[pos] = "x"
            s = "".join(s)
            tol = 2 * c + 2
            fast = approximate_period(s, abs_tol=tol, rel_tol=0.0)
            slow = naive_approximate(s, abs_tol=tol, rel_tol=0.0)
            self.assertEqual(slow is None, fast is None, (s, slow, fast))
            if slow is not None:
                self.assertEqual(fast.period, slow[0], (s, slow, fast))
                self.assertEqual(fast.mismatches, slow[1], (s, slow, fast))
                # 错配位置本身也按定义复核
                for i in fast.mismatch_positions:
                    self.assertNotEqual(s[i], s[i + fast.period])

    def test_no_approx_repeat(self):
        rng = random.Random(342)
        s = "".join(rng.choice("acgt") for _ in range(300))
        self.assertIsNone(approximate_period(s, abs_tol=2, rel_tol=0.0))
        self.assertIsNone(naive_approximate(s, abs_tol=2, rel_tol=0.0))

    def test_heuristic_path_vs_naive(self):
        # n > 2048 走启发式发现路径，仍与朴素枚举对拍
        rng = random.Random(343)
        for _ in range(20):
            L = rng.randint(2, 1300)
            unit = "".join(rng.choice("acgt") for _ in range(L))
            s = list((unit * (2100 // L + 2))[:rng.randint(2049, 3000)])
            c = rng.randint(0, 6)
            for pos in rng.sample(range(len(s)), min(len(s), c)):
                s[pos] = "x"
            s = "".join(s)
            tol = 2 * c + 2
            fast = approximate_period(s, abs_tol=tol, rel_tol=0.0)
            slow = naive_approximate(s, abs_tol=tol, rel_tol=0.0)
            self.assertEqual(slow is None, fast is None, (L, s[:80], slow, fast))
            if slow is not None:
                self.assertEqual(fast.period, slow[0], (L, slow, fast))
                self.assertEqual(fast.mismatches, slow[1], (L, slow, fast))

    def test_sampled_shift_path(self):
        # L > 4096 且腐蚀在首尾：p0 因子与前缀投票均失效，靠位移抽样发现
        rng = random.Random(344)
        unit = "".join(rng.choice("acgt") for _ in range(5000))
        s = list(unit * 20)          # n = 100000
        for pos in [0, 40000, 60001, 99998, 99999]:
            s[pos] = "x"
        s = "".join(s)
        r = approximate_period(s, abs_tol=32, rel_tol=0.0, samples=100000)
        self.assertIsNotNone(r)
        self.assertEqual(r.period, 5000)
        self.assertLessEqual(r.mismatches, 10)

    def test_corruption_near_tail(self):
        # 腐蚀落在最后一个周期内：精确候选退化为 n，靠前缀周期 q* 兜底
        unit = "acgtacg"
        s = list(unit * 30)
        s[-1] = "x"
        s = "".join(s)
        r = approximate_period(s, abs_tol=4, rel_tol=0.0)
        self.assertIsNotNone(r)
        self.assertEqual(r.period, 7)
        self.assertEqual(r.mismatches, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

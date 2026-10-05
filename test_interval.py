# -*- coding: utf-8 -*-
"""interval.py 单元自测：python3 test_interval.py -v"""
import math
import random
import unittest

from interval import (INF, Interval, ZeroContainError, DomainError,
                      hull, compose, exp, log, sqrt, sin)


class TestPointInterval(unittest.TestCase):
    """单点区间。"""

    def test_point_arithmetic(self):
        r = Interval(2) + Interval(3)
        self.assertIn(5.0, r)
        self.assertLess(r.width(), 1e-12)
        r = Interval(2) * Interval(3)
        self.assertIn(6.0, r)
        r = Interval(6) / Interval(3)
        self.assertIn(2.0, r)

    def test_point_pow(self):
        self.assertIn(8.0, Interval(2) ** 3)
        self.assertIn(0.25, Interval(2) ** -2)

    def test_point_zero_div_raises(self):
        with self.assertRaises(ZeroContainError):
            Interval(1) / Interval(0)


class TestSubDegeneracy(unittest.TestCase):
    """减法退化：X - X 不为 0，宽度翻倍（依赖问题的教科书案例）。"""

    def test_x_minus_x(self):
        x = Interval(-1.0, 2.0)
        r = x - x
        self.assertIn(0.0, r)                      # 包含性仍成立
        self.assertAlmostEqual(r.width(), 6.0, places=6)  # 2 * width(X)
        self.assertLess(r.lo, 0.0)
        self.assertGreater(r.hi, 0.0)

    def test_rewrite_avoids_degeneracy(self):
        # 2x - x 朴素计算 vs 化简为 x
        x = Interval(-1.0, 2.0)
        naive = Interval(2) * x - x
        simplified = x
        self.assertGreater(naive.width(), simplified.width())


class TestDivZero(unittest.TestCase):
    """除以含零区间：显式处理，绝不返回看似正常的区间。"""

    def test_straddle_raises(self):
        with self.assertRaises(ZeroContainError):
            Interval(1, 2) / Interval(-1, 1)

    def test_closed_zero_endpoint_raises(self):
        with self.assertRaises(ZeroContainError):
            Interval(1, 2) / Interval(0, 1)
        with self.assertRaises(ZeroContainError):
            Interval(1, 2) / Interval(-1, 0)

    def test_extended_straddle_is_whole_line(self):
        r = Interval(1, 2).div(Interval(-1, 1), extended=True)
        self.assertEqual(r.lo, -INF)
        self.assertEqual(r.hi, INF)

    def test_extended_closed_endpoint_unbounded(self):
        r = Interval(1, 2).div(Interval(0, 1), extended=True)
        self.assertEqual(r.hi, INF)     # [1, +inf)
        self.assertAlmostEqual(r.lo, 1.0, places=12)

    def test_open_zero_endpoint_no_raise(self):
        # (0,1] 不含 0：合法，结果为无界区间 [1, +inf)
        r = Interval(1, 2) / Interval(0, 1, lc=False)
        self.assertEqual(r.hi, INF)
        self.assertAlmostEqual(r.lo, 1.0, places=12)
        self.assertFalse(r.hc)


class TestOpenEndpoints(unittest.TestCase):
    """开区间端点的传播。"""

    def test_add_open(self):
        r = Interval(0, 1, lc=False) + Interval(0, 1)
        self.assertFalse(r.lc)   # (0,1] + [0,1] = (0,2]
        self.assertTrue(r.hc)
        # 向外舍入 1 ulp：lo 为 nextafter(0, -inf)，端点保持开
        self.assertEqual(r.lo, math.nextafter(0.0, -math.inf))
        self.assertIn(2.0, r)

    def test_mul_open(self):
        r = Interval(0, 2, lc=False) * Interval(0, 3, lc=False)
        self.assertFalse(r.lc)
        self.assertEqual(r.lo, math.nextafter(0.0, -math.inf))

    def test_membership_respects_openness(self):
        iv = Interval(0, 1, lc=False, hc=False)
        self.assertNotIn(0.0, iv)
        self.assertNotIn(1.0, iv)
        self.assertIn(0.5, iv)


class TestPowAndFuncs(unittest.TestCase):
    def test_pow_even_with_zero(self):
        r = Interval(-1, 2) ** 2
        self.assertAlmostEqual(r.lo, 0.0)
        self.assertAlmostEqual(r.hi, 4.0)

    def test_pow_odd(self):
        r = Interval(-2, 1) ** 3
        self.assertAlmostEqual(r.lo, -8.0)
        self.assertAlmostEqual(r.hi, 1.0)

    def test_pow_negative_int(self):
        r = Interval(1, 2) ** -1
        self.assertAlmostEqual(r.lo, 0.5)
        self.assertAlmostEqual(r.hi, 1.0)
        with self.assertRaises(DomainError):
            Interval(-1, 1) ** -2

    def test_exp_log_sqrt(self):
        self.assertIn(1.0, exp(Interval(0)))
        self.assertIn(math.e, exp(Interval(1)))
        self.assertIn(0.0, log(Interval(1)))
        self.assertIn(2.0, sqrt(Interval(4)))

    def test_domain_errors(self):
        with self.assertRaises(DomainError):
            log(Interval(-1, 1))
        with self.assertRaises(DomainError):
            sqrt(Interval(-2, 1))

    def test_sin_full_range(self):
        self.assertEqual(sin(Interval(0, 10)).width(), 2.0)  # 宽 >= 2π → [-1,1]
        r = sin(Interval(0, math.pi / 2))
        self.assertAlmostEqual(r.lo, 0.0, places=12)
        self.assertAlmostEqual(r.hi, 1.0, places=12)


class TestComposeAndChain(unittest.TestCase):
    """函数复合与长表达式链的包含性。"""

    def test_compose(self):
        f = compose(lambda x: x ** 2, lambda x: x + 1)  # (x+1)^2
        r = f(Interval(0, 1))
        self.assertIn(1.0, r)
        self.assertIn(4.0, r)

    def test_long_chain_inclusion(self):
        # x <- 1.001*x - 0.001*x（实数上恒等，区间上宽度膨胀）
        rng = random.Random(42)
        x0 = Interval(0.5, 1.5)
        points = [rng.uniform(0.5, 1.5) for _ in range(200)]
        iv = x0
        for _ in range(500):
            iv = Interval(1.001) * iv - Interval(0.001) * iv
        for p in points:
            q = p
            for _ in range(500):
                q = 1.001 * q - 0.001 * q
            self.assertIn(q, iv)
        self.assertGreater(iv.width(), x0.width())  # 宽度确实增长


class TestRandomInclusion(unittest.TestCase):
    """随机包含性验证（单元测试内小规模，完整版见 experiments.py）。"""

    def test_random_ops(self):
        rng = random.Random(2026)
        ops = ["add", "sub", "mul", "div", "pow", "exp", "sin"]
        for _ in range(20000):
            op = rng.choice(ops)
            lo, hi = sorted((rng.uniform(-5, 5), rng.uniform(-5, 5)))
            a = Interval(lo, hi)
            x = rng.uniform(a.lo, a.hi)
            if op == "add":
                lo, hi = sorted((rng.uniform(-5, 5), rng.uniform(-5, 5)))
                b = Interval(lo, hi)
                y = rng.uniform(b.lo, b.hi)
                self.assertIn(x + y, a + b)
            elif op == "sub":
                lo, hi = sorted((rng.uniform(-5, 5), rng.uniform(-5, 5)))
                b = Interval(lo, hi)
                y = rng.uniform(b.lo, b.hi)
                self.assertIn(x - y, a - b)
            elif op == "mul":
                lo, hi = sorted((rng.uniform(-5, 5), rng.uniform(-5, 5)))
                b = Interval(lo, hi)
                y = rng.uniform(b.lo, b.hi)
                self.assertIn(x * y, a * b)
            elif op == "div":
                lo, hi = sorted((rng.uniform(0.5, 5), rng.uniform(0.5, 5)))
                b = Interval(lo, hi)
                y = rng.uniform(b.lo, b.hi)
                self.assertIn(x / y, a / b)
            elif op == "pow":
                n = rng.randint(0, 5)
                self.assertIn(x ** n, a ** n)
            elif op == "exp":
                self.assertIn(math.exp(x), exp(a))
            elif op == "sin":
                self.assertIn(math.sin(x), sin(a))


class TestHull(unittest.TestCase):
    def test_hull(self):
        h = hull(Interval(0, 1), Interval(3, 4), Interval(-2, -1))
        self.assertEqual((h.lo, h.hi), (-2.0, 4.0))


if __name__ == "__main__":
    unittest.main()

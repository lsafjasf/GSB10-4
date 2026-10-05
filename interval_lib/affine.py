"""仿射形式（Affine Arithmetic）：缓解区间宽度膨胀的一阶误差模型。

思想：每个量表示为  x = x0 + Σ xi·εi，εi ∈ [-1, 1] 为共享噪声符号。
- 加减法逐符号合并，相关误差相互抵消（x - x = 0，宽度不增）；
- 乘法只保留一阶项，二阶项合并进一个新噪声符号（保守，保包含性）；
- 非线性函数用切比雪夫线性逼近 + 新噪声符号吸收余项。
"""

import math

from .interval import Interval, IntervalDivisionByZero, IntervalEmptyError


class Affine:
    __slots__ = ("c", "terms")

    _counter = 0

    def __init__(self, c, terms=None):
        self.c = float(c)
        self.terms = dict(terms) if terms else {}

    @classmethod
    def from_interval(cls, iv):
        """区间 → 仿射形式：中点 ± 半径，半径挂一个新噪声符号。"""
        cls._counter += 1
        return cls(iv.mid, {cls._counter: (iv.hi - iv.lo) / 2.0})

    @classmethod
    def _fresh(cls):
        cls._counter += 1
        return cls._counter

    # ---- 转换 ----
    def to_interval(self):
        r = sum(abs(v) for v in self.terms.values())
        return Interval(math.nextafter(self.c - r, -math.inf),
                        math.nextafter(self.c + r, math.inf))

    @property
    def radius(self):
        return sum(abs(v) for v in self.terms.values())

    def __repr__(self):
        return f"Affine(c={self.c!r}, r={self.radius!r}, n={len(self.terms)})"

    # ---- 运算 ----
    @staticmethod
    def _coerce(other):
        if isinstance(other, Affine):
            return other
        if isinstance(other, (int, float)):
            return Affine(float(other))
        return NotImplemented

    def __add__(self, other):
        other = Affine._coerce(other)
        if other is NotImplemented:
            return NotImplemented
        terms = dict(self.terms)
        for k, v in other.terms.items():
            terms[k] = terms.get(k, 0.0) + v
        return Affine(self.c + other.c, terms)

    __radd__ = __add__

    def __neg__(self):
        return Affine(-self.c, {k: -v for k, v in self.terms.items()})

    def __sub__(self, other):
        other = Affine._coerce(other)
        if other is NotImplemented:
            return NotImplemented
        return self + (-other)

    def __rsub__(self, other):
        other = Affine._coerce(other)
        if other is NotImplemented:
            return NotImplemented
        return other + (-self)

    def __mul__(self, other):
        other = Affine._coerce(other)
        if other is NotImplemented:
            return NotImplemented
        c = self.c * other.c
        terms = {}
        for k, v in self.terms.items():
            terms[k] = terms.get(k, 0.0) + v * other.c
        for k, v in other.terms.items():
            terms[k] = terms.get(k, 0.0) + v * self.c
        # 二阶项 Σ|xi|·Σ|yi| 并入新噪声符号（保守上界）
        quad = self.radius * other.radius
        if quad > 0.0:
            terms[Affine._fresh()] = quad
        return Affine(c, terms)

    __rmul__ = __mul__

    def __truediv__(self, other):
        other = Affine._coerce(other)
        if other is NotImplemented:
            return NotImplemented
        return self * other._inv()

    def __rtruediv__(self, other):
        other = Affine._coerce(other)
        if other is NotImplemented:
            return NotImplemented
        return other * self._inv()

    def _inv(self):
        """1/x 的切比雪夫线性逼近，x 的区间不含 0。"""
        iv = self.to_interval()
        if iv.lo <= 0.0 <= iv.hi:
            raise IntervalDivisionByZero(f"affine reciprocal over {iv!r} contains 0")
        a, b = iv.lo, iv.hi
        # f(x)=1/x 在 [a,b]（同号）上的最佳线性逼近：斜率 -1/(a·b)
        alpha = -1.0 / (a * b)
        f_a, f_b = 1.0 / a, 1.0 / b
        zeta = (f_a + f_b) / 2.0 - alpha * (a + b) / 2.0
        delta = abs(f_b - f_a - alpha * (b - a)) / 2.0
        return self._linmap(alpha, zeta, delta)

    def _linmap(self, alpha, zeta, delta):
        """y = alpha·x + zeta ± delta（delta 进新噪声符号）。"""
        terms = {k: v * alpha for k, v in self.terms.items()}
        if delta > 0.0:
            terms[Affine._fresh()] = delta
        return Affine(alpha * self.c + zeta, terms)

    def __pow__(self, n):
        if not isinstance(n, int) or n < 0:
            raise TypeError("affine power supports non-negative integers")
        result = Affine(1.0)
        base = self
        while n:
            if n & 1:
                result = result * base
            base = base * base
            n >>= 1
        return result

    def exp(self):
        """exp 的切比雪夫线性逼近（在值域区间上）。"""
        iv = self.to_interval()
        a, b = iv.lo, iv.hi
        if a == b:
            return Affine(math.exp(a))
        alpha = (math.exp(b) - math.exp(a)) / (b - a)
        # 误差极值点在 exp(x)=alpha 处
        x_star = math.log(alpha)
        e_max = math.exp(x_star) - alpha * x_star
        e_end = (math.exp(a) - alpha * a + math.exp(b) - alpha * b) / 2.0
        zeta = (e_max + e_end) / 2.0
        delta = abs(e_max - e_end) / 2.0
        return self._linmap(alpha, zeta, delta)

    def log(self):
        iv = self.to_interval()
        if iv.lo <= 0.0:
            raise IntervalEmptyError(f"affine log over {iv!r} non-positive")
        a, b = iv.lo, iv.hi
        if a == b:
            return Affine(math.log(a))
        alpha = (math.log(b) - math.log(a)) / (b - a)
        x_star = 1.0 / alpha
        e_max = math.log(x_star) - alpha * x_star
        e_end = (math.log(a) - alpha * a + math.log(b) - alpha * b) / 2.0
        zeta = (e_max + e_end) / 2.0
        delta = abs(e_max - e_end) / 2.0
        return self._linmap(alpha, zeta, delta)

    def sqrt(self):
        iv = self.to_interval()
        if iv.lo < 0.0:
            raise IntervalEmptyError(f"affine sqrt over {iv!r} negative")
        a, b = max(iv.lo, 0.0), iv.hi
        if a == b:
            return Affine(math.sqrt(a))
        alpha = (math.sqrt(b) - math.sqrt(a)) / (b - a)
        x_star = 1.0 / (4.0 * alpha * alpha)
        e_max = math.sqrt(x_star) - alpha * x_star
        e_end = (math.sqrt(a) - alpha * a + math.sqrt(b) - alpha * b) / 2.0
        zeta = (e_max + e_end) / 2.0
        delta = abs(e_max - e_end) / 2.0
        return self._linmap(alpha, zeta, delta)

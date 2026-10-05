"""区间算术核心：端点取舍规则、外向舍入、扩展除法。

端点取舍总规则（保证包含性）：
    每个运算先在实数意义下确定结果端点的“精确候选值”，
    再统一做外向舍入：下界向下舍入、上界向上舍入。
    因此即使浮点有舍入误差，真实值仍严格落在结果区间内。

开/闭端点规则：
    运算结果端点的开闭 = 参与取到该端点的所有源端点开闭的“并”（任一开则开）。
    区间并（如除法过 0 的两支）在端点处不取到 → 端点开。
"""

import math

_INF = math.inf
_ULP = math.ulp(1.0)


def _dn(x):
    """下界舍入：向 -inf 方向挪一个 ulp。"""
    if x == -_INF:
        return x
    if x == _INF:
        raise OverflowError("finite upper bound required for downward rounding")
    return math.nextafter(x, -_INF)


def _up(x):
    """上界舍入：向 +inf 方向挪一个 ulp。"""
    if x == _INF:
        return x
    if x == -_INF:
        raise OverflowError("finite lower bound required for upward rounding")
    return math.nextafter(x, _INF)


class IntervalDivisionByZero(ArithmeticError):
    """除数区间含 0：普通除法拒绝给出“看似正常”的区间。

    需要扩展结果（含无穷的射线并集）时，请显式调用 div_extended()。
    """


class IntervalEmptyError(ArithmeticError):
    """函数在区间上无定义（如 log 作用于含非正数的区间）。"""


class Interval:
    """机器数区间 [lo, hi]，可带开端点。

    lo_open=True 表示左端点不取到（开），hi_open 同理。
    单点区间：Interval.point(x)。
    """

    __slots__ = ("lo", "hi", "lo_open", "hi_open")

    def __init__(self, lo, hi, lo_open=False, hi_open=False):
        lo = float(lo)
        hi = float(hi)
        if math.isnan(lo) or math.isnan(hi):
            raise ValueError("interval endpoints must not be NaN")
        if lo > hi:
            raise ValueError("empty interval: lo > hi")
        if lo == hi and (lo_open or hi_open):
            raise ValueError("empty interval: open point")
        self.lo = lo
        self.hi = hi
        self.lo_open = bool(lo_open)
        self.hi_open = bool(hi_open)

    # ---- 构造 ----
    @classmethod
    def point(cls, x):
        """单点区间 [x, x]。"""
        return cls(x, x)

    @classmethod
    def hull(cls, a, b):
        """含 a、b 的最小区间。"""
        return cls(min(a, b), max(a, b))

    # ---- 基本性质 ----
    @property
    def width(self):
        return self.hi - self.lo

    @property
    def mid(self):
        return self.lo + (self.hi - self.lo) / 2.0

    @property
    def is_point(self):
        return self.lo == self.hi

    @property
    def is_finite(self):
        return math.isfinite(self.lo) and math.isfinite(self.hi)

    def __contains__(self, x):
        if x < self.lo or x > self.hi:
            return False
        if self.lo_open and x == self.lo:
            return False
        if self.hi_open and x == self.hi:
            return False
        return True

    def contains_interval(self, other):
        """self ⊇ other（含端点开闭语义）。"""
        if other.lo < self.lo or other.hi > self.hi:
            return False
        if other.lo == self.lo and self.lo_open and not other.lo_open:
            return False
        if other.hi == self.hi and self.hi_open and not other.hi_open:
            return False
        return True

    def __repr__(self):
        lb = "(" if self.lo_open else "["
        rb = ")" if self.hi_open else "]"
        return f"{lb}{self.lo!r}, {self.hi!r}{rb}"

    def __eq__(self, other):
        if not isinstance(other, Interval):
            return NotImplemented
        return (
            self.lo == other.lo
            and self.hi == other.hi
            and self.lo_open == other.lo_open
            and self.hi_open == other.hi_open
        )

    __hash__ = None

    # ---- 内部工具 ----
    @staticmethod
    def _coerce(other):
        if isinstance(other, Interval):
            return other
        if isinstance(other, (int, float)):
            return Interval.point(other)
        return NotImplemented

    @staticmethod
    def _endpoints_product(a, b):
        """乘法端点取舍：4 个端点积取 min/max，下界下舍、上界上舍。

        0 x ±inf（扩展除法构造射线时才会出现）按集合语义处理：
        0 本身贡献 0；0 所在区间的内部符号决定商向哪侧无界。
        """
        cands = []
        for x, xo, xint in ((a.lo, a.lo_open, a), (a.hi, a.hi_open, a)):
            for y, yo, yint in ((b.lo, b.lo_open, b), (b.hi, b.hi_open, b)):
                p = x * y
                if p != p:  # NaN: 0 * inf
                    cands.append((0.0, False))
                    if x == 0.0:
                        # x=0 端点、y 无界：xint 内部近 0 的值与无界 y 决定无界方向
                        if xint.hi > 0.0:
                            cands.append((_INF if y > 0 else -_INF, False))
                        if xint.lo < 0.0:
                            cands.append((-_INF if y > 0 else _INF, False))
                    if y == 0.0:
                        if yint.hi > 0.0:
                            cands.append((_INF if x > 0 else -_INF, False))
                        if yint.lo < 0.0:
                            cands.append((-_INF if x > 0 else _INF, False))
                else:
                    cands.append((p, xo or yo))
        lo_val = min(c[0] for c in cands)
        hi_val = max(c[0] for c in cands)
        lo_open = all(c[1] for c in cands if c[0] == lo_val)
        hi_open = all(c[1] for c in cands if c[0] == hi_val)
        lo = lo_val if lo_val == -_INF else _dn(lo_val)
        hi = hi_val if hi_val == _INF else _up(hi_val)
        return Interval(lo, hi, lo_open, hi_open)

    def _recip(self):
        """1/x，要求 self 不含 0。端点互换：1/[a,b] = [1/b, 1/a]。"""
        lo, hi = self.lo, self.hi
        if lo == 0.0 and hi == 0.0:
            raise IntervalDivisionByZero("1/[0,0] is undefined")
        # 上端 = 1/lo（lo 可能为 0+ → +inf 射线）
        if lo == 0.0:
            r_hi, r_hi_open = _INF, False
        else:
            r_hi, r_hi_open = _up(1.0 / lo), self.lo_open
        # 下端 = 1/hi
        if hi == 0.0:
            r_lo, r_lo_open = -_INF, False
        else:
            r_lo, r_lo_open = _dn(1.0 / hi), self.hi_open
        return Interval(r_lo, r_hi, r_lo_open, r_hi_open)

    def _contains_zero(self):
        return self.lo <= 0.0 <= self.hi

    # ---- 四则运算 ----
    def __add__(self, other):
        other = Interval._coerce(other)
        if other is NotImplemented:
            return NotImplemented
        lo = _dn(self.lo + other.lo) if math.isfinite(self.lo + other.lo) else -_INF
        hi = _up(self.hi + other.hi) if math.isfinite(self.hi + other.hi) else _INF
        return Interval(lo, hi, self.lo_open or other.lo_open, self.hi_open or other.hi_open)

    __radd__ = __add__

    def __sub__(self, other):
        other = Interval._coerce(other)
        if other is NotImplemented:
            return NotImplemented
        lo = _dn(self.lo - other.hi) if math.isfinite(self.lo - other.hi) else -_INF
        hi = _up(self.hi - other.lo) if math.isfinite(self.hi - other.lo) else _INF
        return Interval(lo, hi, self.lo_open or other.hi_open, self.hi_open or other.lo_open)

    def __rsub__(self, other):
        other = Interval._coerce(other)
        if other is NotImplemented:
            return NotImplemented
        return other.__sub__(self)

    def __neg__(self):
        return Interval(-self.hi, -self.lo, self.hi_open, self.lo_open)

    def __mul__(self, other):
        other = Interval._coerce(other)
        if other is NotImplemented:
            return NotImplemented
        return Interval._endpoints_product(self, other)

    __rmul__ = __mul__

    def __truediv__(self, other):
        """普通除法：除数含 0 时抛 IntervalDivisionByZero（显式失败）。

        不允许静默返回“看似正常”的区间；需要扩展语义请用 div_extended()。
        """
        other = Interval._coerce(other)
        if other is NotImplemented:
            return NotImplemented
        if other._contains_zero():
            raise IntervalDivisionByZero(
                f"divisor interval {other!r} contains 0; "
                "use div_extended() for an explicit unbounded result"
            )
        return Interval._endpoints_product(self, other._recip())

    def __rtruediv__(self, other):
        other = Interval._coerce(other)
        if other is NotImplemented:
            return NotImplemented
        return other.__truediv__(self)

    # ---- 幂 ----
    def __pow__(self, n):
        """整数幂。n<0 时要求区间不含 0（否则抛 IntervalDivisionByZero）。"""
        if not isinstance(n, int):
            raise TypeError("interval power supports integer exponents only")
        if n == 0:
            return Interval.point(1.0)
        if n < 0:
            if self._contains_zero():
                raise IntervalDivisionByZero(
                    f"x^{n} with x straddling 0 is unbounded; interval {self!r}"
                )
            return (self._recip()) ** (-n)
        if n % 2 == 1:
            # 奇次幂单调增：端点直接映射
            return Interval(_dn(self.lo ** n), _up(self.hi ** n), self.lo_open, self.hi_open)
        # 偶次幂：含 0 则下界为 0（闭，因为 0 取到），否则取端点 min
        a, b = self.lo ** n, self.hi ** n
        if self.lo <= 0.0 <= self.hi:
            return Interval(0.0, _up(max(a, b)), False, self.lo_open or self.hi_open)
        lo_val, hi_val = min(a, b), max(a, b)
        lo_open = self.lo_open if a <= b else self.hi_open
        hi_open = self.hi_open if b >= a else self.lo_open
        return Interval(_dn(lo_val), _up(hi_val), lo_open, hi_open)

    # ---- 初等函数（函数复合 = 运算的连续应用，规则逐层传递）----
    def _map_monotone(self, f, increasing=True):
        """单调函数：端点直接映射 + 外向舍入。"""
        flo, fhi = f(self.lo), f(self.hi)
        if increasing:
            return Interval(_dn(flo), _up(fhi), self.lo_open, self.hi_open)
        return Interval(_dn(fhi), _up(flo), self.hi_open, self.lo_open)

    def exp(self):
        return self._map_monotone(math.exp, increasing=True)

    def log(self):
        if self.hi <= 0.0:
            raise IntervalEmptyError(f"log undefined on {self!r}")
        lo = _dn(math.log(self.lo)) if self.lo > 0.0 else -_INF
        return Interval(lo, _up(math.log(self.hi)), self.lo <= 0.0 or self.lo_open, self.hi_open)

    def sqrt(self):
        if self.hi < 0.0:
            raise IntervalEmptyError(f"sqrt undefined on {self!r}")
        lo = _dn(math.sqrt(self.lo)) if self.lo >= 0.0 else 0.0
        return Interval(lo, _up(math.sqrt(self.hi)), self.lo < 0.0 or self.lo_open, self.hi_open)

    def sin(self):
        return self._map_periodic(math.sin, period=2.0 * math.pi, extrema_at=math.pi / 2.0)

    def cos(self):
        return self._map_periodic(math.cos, period=2.0 * math.pi, extrema_at=0.0)

    def _map_periodic(self, f, period, extrema_at):
        """周期函数：若区间覆盖某极值点则取 ±1，否则取端点值 min/max。"""
        lo, hi = self.lo, self.hi
        flo, fhi = f(lo), f(hi)
        lo_val, hi_val = min(flo, fhi), max(flo, fhi)
        lo_open = self.lo_open if flo <= fhi else self.hi_open
        hi_open = self.hi_open if fhi >= flo else self.lo_open
        if hi - lo >= period:
            return Interval(-1.0, 1.0)
        k = math.ceil((lo - extrema_at) / (period / 2.0))
        while extrema_at + k * (period / 2.0) <= hi:
            x = extrema_at + k * (period / 2.0)
            v = f(x)
            if v < lo_val:
                lo_val, lo_open = v, False
            if v > hi_val:
                hi_val, hi_open = v, False
            k += 1
        return Interval(_dn(lo_val), _up(hi_val), lo_open, hi_open)

    def abs(self):
        if self.lo <= 0.0 <= self.hi:
            lo, lo_open = 0.0, False
        else:
            lo = min(abs(self.lo), abs(self.hi))
            lo_open = self.lo_open if abs(self.lo) <= abs(self.hi) else self.hi_open
        hi = max(abs(self.lo), abs(self.hi))
        hi_open = self.hi_open if abs(self.hi) >= abs(self.lo) else self.lo_open
        return Interval(lo, hi, lo_open, hi_open)


class Ray(Interval):
    """单边无界区间（射线），如 (2, +inf]。仅用于扩展除法结果。"""

    @classmethod
    def above(cls, a, open_at_a=True):
        return cls(a, _INF, open_at_a, False)

    @classmethod
    def below(cls, b, open_at_b=True):
        return cls(-_INF, b, False, open_at_b)


class IntervalUnion:
    """区间之并：扩展除法过 0 时的显式结果，绝不伪装成普通区间。"""

    __slots__ = ("parts",)

    def __init__(self, parts):
        self.parts = tuple(parts)

    @property
    def is_bounded(self):
        return all(p.is_finite for p in self.parts)

    def __contains__(self, x):
        return any(x in p for p in self.parts)

    def __repr__(self):
        return " ∪ ".join(repr(p) for p in self.parts)


def div_extended(a, b):
    """扩展除法 a / b，允许 b 含 0。

    返回 Interval 或 IntervalUnion（含射线），结果类型本身就标明无界性。
    b = [0,0] 时抛 IntervalDivisionByZero（任何非零数除以 0 无意义）。
    """
    a = Interval._coerce(a)
    b = Interval._coerce(b)
    if b.lo == 0.0 and b.hi == 0.0:
        raise IntervalDivisionByZero("division by the point interval [0, 0]")
    if not b._contains_zero():
        return a / b
    parts = []
    if b.lo < 0.0:
        # b 的负支 [lo, 0) → 倒数为 (-inf, 1/lo)
        neg = Interval(b.lo, 0.0, b.lo_open, True)._recip()
        parts.append(Interval._endpoints_product(a, neg))
    if b.hi > 0.0:
        pos = Interval(0.0, b.hi, True, b.hi_open)._recip()
        parts.append(Interval._endpoints_product(a, pos))
    if not parts:  # b == [0,0] 已在上面拦截，此处防御
        raise IntervalDivisionByZero("division by the point interval [0, 0]")
    if len(parts) == 1:
        return parts[0]
    return IntervalUnion(parts)

"""区间算术库（仅标准库）。

设计原则：包含性（inclusion）第一。对库中任意运算 op 与 x ∈ X, y ∈ Y，
保证 op(x, y) ∈ op(X, Y)。为此：

1. 所有运算结果端点用 math.nextafter 向外舍入 1 个 ulp，抵消浮点舍入误差；
2. 端点开闭（lc/hc）显式记录并传播；无法精确判定时保守取闭——
   闭化只会放大区间，绝不破坏包含性；
3. 除以"达到 0"的除数区间默认抛 ZeroContainError，绝不返回看似正常的
   有界区间；显式 extended=True 时返回含 ±inf 的无界结果（或整个实轴）；
4. 定义域违规（log/sqrt 负数、0 的负整数幂等）抛 DomainError；
5. 支持 ±inf 端点（必为开），用于扩展除法与无界区间。
"""

from __future__ import annotations

import math

INF = math.inf

__all__ = [
    "Interval", "IntervalError", "ZeroContainError", "DomainError",
    "hull", "compose", "exp", "log", "sqrt", "sin", "cos",
]


class IntervalError(Exception):
    """区间算术错误基类。"""


class ZeroContainError(IntervalError):
    """除数区间达到 0（0 在内部或闭端点上）。"""


class DomainError(IntervalError):
    """运算在区间上无定义（如 log 非正数、0 的负幂）。"""


def _out_lo(x):
    return math.nextafter(x, -INF)


def _out_hi(x):
    return math.nextafter(x, INF)


def _zero_attained(iv):
    """0 是否为区间内可达到的点。"""
    return ((iv.lo < 0 < iv.hi)
            or (iv.lo == 0 and iv.lc)
            or (iv.hi == 0 and iv.hc))


def _coerce(x):
    if isinstance(x, Interval):
        return x
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise TypeError(f"无法转换为区间: {type(x)!r}")
    return Interval(x)


def _safe_exp(x):
    try:
        return math.exp(x)
    except OverflowError:
        return INF


class Interval:
    """区间 [lo, hi]，端点开闭由 lc/hc 标记。±inf 端点强制为开。"""

    __slots__ = ("lo", "hi", "lc", "hc")

    def __init__(self, lo, hi=None, lc=True, hc=True):
        if hi is None:
            hi = lo
        lo = float(lo)
        hi = float(hi)
        if math.isnan(lo) or math.isnan(hi):
            raise ValueError("端点不能为 NaN")
        if lo > hi:
            raise ValueError(f"空区间: lo={lo} > hi={hi}")
        if lo == -INF:
            lc = False
        if hi == INF:
            hc = False
        if lo == hi and not (lc and hc):
            raise ValueError("空区间: 单点但端点开")
        self.lo = lo
        self.hi = hi
        self.lc = bool(lc)
        self.hc = bool(hc)

    # ---- 基本性质 ----

    def width(self):
        return self.hi - self.lo

    def mid(self):
        if self.lo == -INF or self.hi == INF:
            raise IntervalError("无界区间无中点")
        return self.lo / 2 + self.hi / 2

    def is_point(self):
        return self.lo == self.hi

    def __contains__(self, x):
        x = float(x)
        return ((self.lo < x or (self.lc and self.lo == x))
                and (x < self.hi or (self.hc and self.hi == x)))

    def __eq__(self, other):
        return (isinstance(other, Interval)
                and (self.lo, self.hi, self.lc, self.hc)
                == (other.lo, other.hi, other.lc, other.hc))

    def __repr__(self):
        left = "[" if self.lc else "("
        right = "]" if self.hc else ")"
        return f"{left}{self.lo!r}, {self.hi!r}{right}"

    # ---- 四则运算 ----

    def add(self, other):
        other = _coerce(other)
        return Interval(_out_lo(self.lo + other.lo),
                        _out_hi(self.hi + other.hi),
                        self.lc and other.lc, self.hc and other.hc)

    def sub(self, other):
        other = _coerce(other)
        return Interval(_out_lo(self.lo - other.hi),
                        _out_hi(self.hi - other.lo),
                        self.lc and other.hc, self.hc and other.lc)

    def mul(self, other):
        other = _coerce(other)
        a, b = self, other
        if (a.lo == 0 and a.hi == 0) or (b.lo == 0 and b.hi == 0):
            return Interval(0.0, 0.0)
        cand = []
        for va, fa in ((a.lo, a.lc), (a.hi, a.hc)):
            for vb, fb in ((b.lo, b.lc), (b.hi, b.hc)):
                p = va * vb
                if p != p:  # 0 * ±inf = NaN：按角点邻域符号补 ±inf 与 0
                    if va == 0:
                        z_sign = 1.0 if a.lo == 0 else -1.0
                        s = math.copysign(1.0, vb) * z_sign
                    else:
                        z_sign = 1.0 if b.lo == 0 else -1.0
                        s = math.copysign(1.0, va) * z_sign
                    cand.append((0.0, False))
                    cand.append((s * INF, False))
                else:
                    cand.append((p, fa and fb))
        lo_v = min(v for v, _ in cand)
        hi_v = max(v for v, _ in cand)
        lc = all(f for v, f in cand if v == lo_v)
        hc = all(f for v, f in cand if v == hi_v)
        return Interval(_out_lo(lo_v), _out_hi(hi_v), lc, hc)

    def div(self, other, extended=False):
        """self / other。

        除数达到 0 时：extended=False（默认）抛 ZeroContainError；
        extended=True 返回无界区间（0 跨内部时为整个实轴，即双支的凸包）。
        """
        other = _coerce(other)
        return self * _recip(other, extended=extended)

    def neg(self):
        return Interval(-self.hi, -self.lo, self.hc, self.lc)

    def abs(self):
        if self.lo >= 0:
            return self
        if self.hi <= 0:
            return Interval(-self.hi, -self.lo, self.hc, self.lc)
        return Interval(0.0, max(-self.lo, self.hi), False, True)

    # ---- 幂 ----

    def _pow_int(self, n):
        if n == 0:
            return Interval(1.0, 1.0)  # 约定 0**0 = 1
        if n < 0:
            if _zero_attained(self):
                raise DomainError("0 的负整数幂无定义")
            return _recip(self._pow_int(-n), extended=False)
        if n % 2 == 1:
            return Interval(_out_lo(self.lo ** n), _out_hi(self.hi ** n),
                            self.lc, self.hc)
        if self.lo >= 0:
            return Interval(_out_lo(self.lo ** n), _out_hi(self.hi ** n),
                            self.lc, self.hc)
        if self.hi <= 0:
            return Interval(_out_lo(self.hi ** n), _out_hi(self.lo ** n),
                            self.hc, self.lc)
        peak = max(self.lo ** n, self.hi ** n)
        return Interval(0.0, _out_hi(peak), False, True)

    def _pow_general(self, y):
        """x**y = exp(y * ln(x))，要求 x > 0。"""
        if self.lo <= 0:
            raise DomainError("非整数（或区间）指数要求底数区间严格为正")
        return (y * self.log()).exp()

    def pow(self, other):
        if isinstance(other, Interval):
            if other.is_point() and other.lo == int(other.lo) and abs(other.lo) < 1e15:
                return self._pow_int(int(other.lo))
            return self._pow_general(other)
        if isinstance(other, int) and not isinstance(other, bool):
            return self._pow_int(other)
        if isinstance(other, float):
            if other.is_integer() and abs(other) < 1e15:
                return self._pow_int(int(other))
            return self._pow_general(Interval(other))
        raise TypeError(f"不支持的指数类型: {type(other)!r}")

    # ---- 初等函数 ----

    def exp(self):
        lo_v = _safe_exp(self.lo) if self.lo > -INF else 0.0
        hi_v = _safe_exp(self.hi) if self.hi < INF else INF
        return Interval(_out_lo(lo_v), _out_hi(hi_v),
                        self.lc and self.lo > -INF, self.hc and self.hi < INF)

    def log(self):
        if self.hi <= 0:
            raise DomainError("log: 区间整体非正")
        if self.lo < 0 or (self.lo == 0 and self.lc):
            raise DomainError("log: 区间含有可达到的非正数")
        lo_v = math.log(self.lo) if self.lo > 0 else -INF
        hi_v = math.log(self.hi) if self.hi < INF else INF
        return Interval(_out_lo(lo_v), _out_hi(hi_v),
                        self.lc and self.lo > 0, self.hc and self.hi < INF)

    def sqrt(self):
        if self.hi < 0:
            raise DomainError("sqrt: 区间整体为负")
        if self.lo < 0:
            raise DomainError("sqrt: 区间含有可达到的负数")
        hi_v = math.sqrt(self.hi) if self.hi < INF else INF
        return Interval(_out_lo(math.sqrt(self.lo)), _out_hi(hi_v),
                        self.lc, self.hc and self.hi < INF)

    def sin(self):
        two_pi = 2 * math.pi
        if self.width() >= two_pi:
            return Interval(-1.0, 1.0)

        def crosses(c):
            k = math.ceil((self.lo - c) / two_pi)
            return c + k * two_pi <= self.hi

        hi_v = 1.0 if crosses(math.pi / 2) else max(math.sin(self.lo),
                                                    math.sin(self.hi))
        lo_v = -1.0 if crosses(3 * math.pi / 2) else min(math.sin(self.lo),
                                                         math.sin(self.hi))
        return Interval(_out_lo(lo_v), _out_hi(hi_v))

    def cos(self):
        return (self + Interval(math.pi / 2)).sin()

    # ---- 运算符重载 ----

    def __add__(self, other):
        return self.add(other)

    def __radd__(self, other):
        return _coerce(other).add(self)

    def __sub__(self, other):
        return self.sub(other)

    def __rsub__(self, other):
        return _coerce(other).sub(self)

    def __mul__(self, other):
        return self.mul(other)

    def __rmul__(self, other):
        return _coerce(other).mul(self)

    def __truediv__(self, other):
        return self.div(other)

    def __rtruediv__(self, other):
        return _coerce(other).div(self)

    def __pow__(self, other):
        return self.pow(other)

    def __rpow__(self, other):
        if isinstance(other, Interval):
            return other._pow_general(self)
        base = float(other)
        if base <= 0:
            raise DomainError("a**X（区间为指数）要求底数 a > 0")
        return (self * math.log(base)).exp()

    def __neg__(self):
        return self.neg()

    def __abs__(self):
        return self.abs()


def _recip(b, extended=False):
    """1/b。0 达到时按 extended 标志处理。"""
    if b.lo == 0 and b.hi == 0:
        raise ZeroContainError("除数为点区间 [0, 0]，商无意义")
    if _zero_attained(b):
        if not extended:
            raise ZeroContainError(
                f"除数区间 {b} 达到 0；显式 extended=True 可得无界结果")
        if b.lo < 0 < b.hi:
            return Interval(-INF, INF)  # 双支 (-inf,1/lo]∪[1/hi,inf) 的凸包
        if b.lo == 0:
            return Interval(_out_lo(1.0 / b.hi), INF, b.hc, False)
        return Interval(-INF, _out_hi(1.0 / b.lo), False, b.lc)
    if b.lo >= 0:  # b 严格为正（lo 可为开的 0）
        lo_v = 1.0 / b.hi if b.hi != INF else 0.0
        hi_v = 1.0 / b.lo if b.lo > 0 else INF
        return Interval(_out_lo(lo_v), _out_hi(hi_v),
                        b.hc and b.hi != INF, b.lc and b.lo > 0)
    lo_v = 1.0 / b.hi if b.hi < 0 else -INF
    hi_v = 1.0 / b.lo if b.lo != -INF else 0.0
    return Interval(_out_lo(lo_v), _out_hi(hi_v),
                    b.hc and b.hi < 0, b.lc and b.lo != -INF)


def hull(*ivs):
    """若干区间的凸包（最小包含区间），用于区间分裂法合并。"""
    ivs = [_coerce(i) for i in ivs]
    lo = min(i.lo for i in ivs)
    hi = max(i.hi for i in ivs)
    lc = all(i.lc for i in ivs if i.lo == lo)
    hc = all(i.hc for i in ivs if i.hi == hi)
    return Interval(lo, hi, lc, hc)


def compose(*funcs):
    """compose(f, g)(x) == f(g(x))。包含性由逐步包含归纳保证。"""
    def composed(x):
        for fn in reversed(funcs):
            x = fn(x)
        return x
    return composed


def exp(x):
    return _coerce(x).exp()


def log(x):
    return _coerce(x).log()


def sqrt(x):
    return _coerce(x).sqrt()


def sin(x):
    return _coerce(x).sin()


def cos(x):
    return _coerce(x).cos()

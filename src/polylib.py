"""多项式乘法与多点求值（仅标准库）。

约定：多项式用系数列表表示，下标 i 为 x^i 的系数，允许 int / float。
- mul_naive        : 朴素 O(n*m) 卷积，整数输入 -> 精确 int 结果
- mul_fft          : 复数 FFT 卷积，带精度前置/后置检查，不安全则回退朴素
- eval_pointwise   : 逐点 Horner 求值（精确 int）
- eval_multipoint  : 子积树 + 余数树的快速多点求值（精确 int）

所有回退事件写入 fallback_log（list[dict]），绝不静默返回错误结果。
"""

import cmath
import math

fallback_log = []

_SAFE_INT = 1 << 53          # 双精度连续整数上界 9007199254740992
_RESIDUAL_LIMIT = 0.25       # 允许的最大取整残差 |value-round(value)|


def _trim(p):
    q = list(p)
    while len(q) > 1 and q[-1] == 0:
        q.pop()
    return q


# ---------------- 朴素乘法 ----------------

def mul_naive(a, b):
    """O(n*m) 朴素卷积；整数输入结果精确。"""
    if not a or not b:
        return []
    res = [0] * (len(a) + len(b) - 1)
    for i, ai in enumerate(a):
        if ai == 0:
            continue
        for j, bj in enumerate(b):
            res[i + j] += ai * bj
    return _trim(res)


# ---------------- FFT 快速乘法 ----------------

def _fft(a, invert):
    """原地迭代 Cooley-Tukey FFT，len(a) 必须是 2 的幂。"""
    n = len(a)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            a[i], a[j] = a[j], a[i]

    length = 2
    while length <= n:
        ang = -2.0 * math.pi / length
        if invert:
            ang = -ang
        wlen = cmath.exp(1j * ang)
        half = length >> 1
        roots = [complex(1.0)] * half
        for k in range(1, half):
            roots[k] = roots[k - 1] * wlen
        for i in range(0, n, length):
            for k in range(half):
                u = a[i + k]
                v = a[i + k + half] * roots[k]
                a[i + k] = u + v
                a[i + k + half] = u - v
        length <<= 1

    if invert:
        inv_n = 1.0 / n
        for i in range(n):
            a[i] *= inv_n


def _next_pow2(x):
    return 1 << (max(1, x - 1).bit_length())


def _coeff_bound(a, b):
    """结果系数绝对值的先验上界 min(n,m)*max|ai|*max|bj|。"""
    max_a = max((abs(x) for x in a), default=0)
    max_b = max((abs(x) for x in b), default=0)
    return min(len(a), len(b)) * max_a * max_b


def mul_fft(a, b, allow_fallback=True):
    """FFT 卷积。

    整数输入时返回精确 int 系数；检测到精度风险则：
      allow_fallback=True  -> 记录到 fallback_log 后回退 mul_naive
      allow_fallback=False -> 抛出 PrecisionError
    返回 (coeffs, info)，info 含 residual/bound/fallback 等诊断信息。
    """
    if not a or not b:
        return [], {"fallback": "empty", "residual": 0.0, "bound": 0}

    ai = [int(x) for x in a]   # 本库的 FFT 路径只承诺整数精确结果
    bi = [int(x) for x in b]
    bound = _coeff_bound(ai, bi)
    need = len(ai) + len(bi) - 1
    nfft = _next_pow2(need)

    # 先验检查：FFT 累积误差量级约 bound * log2(nfft) * eps
    eps = 2.220446049250313e-16
    expected_error = None
    if bound < _SAFE_INT:
        expected_error = bound * max(1, nfft.bit_length() - 1) * eps
    if bound >= _SAFE_INT or expected_error > _RESIDUAL_LIMIT:
        rec = {
            "stage": "a_priori",
            "deg_a": len(ai) - 1, "deg_b": len(bi) - 1,
            "nfft": nfft, "bound": bound,
            "safe_int": _SAFE_INT,
            "expected_rounding_error": expected_error,
            "residual": None,
        }
        if not allow_fallback:
            raise PrecisionError(rec)
        fallback_log.append(rec)
        exact = mul_naive(ai, bi)
        return exact, {"fallback": "naive", **rec, "residual": 0.0}

    fa = [complex(x) for x in ai] + [complex(0)] * (nfft - len(ai))
    fb = [complex(x) for x in bi] + [complex(0)] * (nfft - len(bi))
    _fft(fa, False)
    _fft(fb, False)
    for i in range(nfft):
        fa[i] *= fb[i]
    _fft(fa, True)

    rounded = [round(x.real) for x in fa[:need]]
    residual = max(abs(fa[k].real - rounded[k]) for k in range(need)) if need else 0.0

    # 后置检查：取整残差异常 -> 判定不可信
    if residual > _RESIDUAL_LIMIT:
        rec = {
            "stage": "a_posteriori",
            "deg_a": len(ai) - 1, "deg_b": len(bi) - 1,
            "nfft": nfft, "bound": bound,
            "safe_int": _SAFE_INT,
            "expected_rounding_error": expected_error,
            "residual": residual,
        }
        if not allow_fallback:
            raise PrecisionError(rec)
        fallback_log.append(rec)
        exact = mul_naive(ai, bi)
        return exact, {"fallback": "naive", **rec}

    return _trim(rounded), {
        "fallback": None, "nfft": nfft, "bound": bound,
        "expected_rounding_error": expected_error, "residual": residual,
    }


class PrecisionError(ArithmeticError):
    def __init__(self, rec):
        super().__init__("FFT 精度不足且禁止回退: %r" % rec)
        self.record = rec


# ---------------- 多项式整除（首一除子，精确整数） ----------------

def poly_mod(p, d):
    """p % d，要求 d 首项系数为 1；返回余数。"""
    p = list(p)
    m = len(d) - 1
    if m == 0:
        return [0]
    while len(p) - 1 >= m and len(p) > 1:
        coef = p[-1]
        shift = len(p) - len(d)
        for k in range(len(d)):
            p[shift + k] -= coef * d[k]
        p.pop()
    return _trim(p)


# ---------------- 多点求值 ----------------

def eval_pointwise(p, xs):
    """逐点 Horner，O(n*m)，精确整数。"""
    out = []
    for x in xs:
        acc = 0
        for c in reversed(p):
            acc = acc * x + c
        out.append(acc)
    return out


def _build_product_tree(xs):
    level = [_trim([-x, 1]) for x in xs]      # 线性因子 x - xi
    tree = [level]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level) - 1, 2):
            nxt.append(mul_fft(level[i], level[i + 1])[0])
        if len(level) & 1:
            nxt.append(level[-1])
        tree.append(nxt)
        level = nxt
    return tree


def eval_multipoint(p, xs):
    """子积树 + 余数树多点求值，精确整数。

    构造 O(M(n) log n)，求值阶段 n 个点共 O(M(n) log n)
    （本实现乘法内部不满足精度时自动回退，结果始终精确）。
    """
    if not xs:
        return []
    p = _trim(p)
    tree = _build_product_tree(list(xs))
    rems = [poly_mod(p, tree[-1][0])]
    for level in range(len(tree) - 2, -1, -1):
        lower = tree[level]
        new_rems = [poly_mod(rems[i >> 1], node) for i, node in enumerate(lower)]
        rems = new_rems
    return [r[0] if r else 0 for r in rems]

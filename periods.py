"""精确周期与近似重复检测（Python 3，仅标准库）。

==================== 定义（两套，严格分开） ====================

精确周期（exact period）
    设 s 长度为 n。若整数 p（1 <= p <= n）满足
        对所有 0 <= i < n - p：s[i] == s[i+p]
    则称 p 是 s 的一个周期。最小的这样的 p 称为 s 的精确周期。
    判定依据：p 是周期  <=>  n - p 是 s 的 border（相等的真前后缀）长度。
    因此 精确周期 = n - 最长真 border 长度 = n - pi[n-1]，
    其中 pi 为 KMP 前缀函数。p = n 时条件为空、必然成立，
    故精确周期永远存在；结果为 n 表示没有任何非平凡重复结构。

近似重复（approximate repetition）
    仅对 p <= n // 2 讨论（保证重复单元至少完整出现两次，否则"重复"无意义）。
    令 m(p) = #{ 0 <= i < n - p : s[i] != s[i+p] }（滑动一位错配计数）。
    若 m(p) <= k，则称 s 在容差 k 下是周期 p 的近似重复。
    容差 k = max(abs_tol, floor(rel_tol * (n - p)))，其中 n - p 是比较位置总数。
    检测结果给出：近似周期 p、错配数 m(p)、全部错配位置、容差 k。

容差如何选定
    若预期串中至多有 c 个被腐蚀的位置，每个位置最多影响 2 个比较位
    （i = t 与 i = t - p），故 m(p) <= 2c，abs_tol 取 >= 2c 即可覆盖；
    腐蚀数量未知但错误率有界时，用 rel_tol（默认 1%）按比较位置数缩放。
    两者取 max，小串靠绝对下限、大串靠相对比例。

近似周期的发现是启发式、验证是精确的：返回值一定满足 m(p) <= k。
候选来源：n <= 2048 时按定义枚举全部 p（与朴素实现逐位一致）；
更大时综合 (1) 精确周期候选 p0 = n - pi[n-1] 的因子，
(2) 前缀周期 q(i) = i+1-pi[i] 的投票高票者（真实周期 L 的倍数在
干净区域反复出现），(3) 位移投票（<=4096 穷举 + 更大范围固定种子抽样，
每个位移 32 个探针粗筛）。腐蚀极少时 L 的倍数必然高票/幸存。

边界情形（都有确定结果）
    空串：精确周期定义为 0；近似重复返回 None。
    单字符：精确周期 1（== 串长）；近似重复 None（n < 2 无法重复两次）。
    周期为串长本身：精确周期 == n，表示无非平凡周期。
    全同字符：精确周期 1；近似周期 1，错配 0 处。
"""

import random
from array import array
from math import gcd

__all__ = [
    "exact_period",
    "approximate_period",
    "ApproxResult",
    "naive_exact_period",
    "naive_approximate",
]

_INT_ARRAY = "i" if array("i").itemsize >= 4 else "l"

_EXACT_UPTO = 2048      # 小于等于此长度时近似检测退化为按定义完整枚举
_EXHAUSTIVE_SHIFT = 4096  # 大于 _EXACT_UPTO 时，此范围内的位移逐个探针粗筛
_PROBES = 32            # 每个位移的探针数（粗筛）


def _prefix_function(s):
    """KMP 前缀函数：pi[i] = s[:i+1] 的最长真 border 长度。O(n)。"""
    n = len(s)
    pi = array(_INT_ARRAY, [0]) * n
    k = 0
    for i in range(1, n):
        c = s[i]
        while k and s[k] != c:
            k = pi[k - 1]
        if s[k] == c:
            k += 1
        pi[i] = k
    return pi


def exact_period(s):
    """精确周期：最小的 p 使 s[i] == s[i+p] 对所有 0 <= i < n-p 成立。

    空串返回 0；否则返回值在 [1, n] 内，n 表示无非平凡周期。
    """
    n = len(s)
    if n == 0:
        return 0
    pi = _prefix_function(s)
    return n - pi[n - 1]


def _divisors(x):
    divs = []
    d = 1
    while d * d <= x:
        if x % d == 0:
            divs.append(d)
            if d != x // d:
                divs.append(x // d)
        d += 1
    return divs


def _mismatch_positions(s, p, limit):
    """返回所有 s[i] != s[i+p] 的位置；超过 limit 个则提前中止并返回 None。"""
    out = []
    for i in range(len(s) - p):
        if s[i] != s[i + p]:
            out.append(i)
            if len(out) > limit:
                return None
    return out


class ApproxResult:
    """近似重复检测结果。"""

    __slots__ = ("period", "compared", "tolerance", "mismatch_positions")

    def __init__(self, period, compared, tolerance, mismatch_positions):
        self.period = period                  # 近似周期 p
        self.compared = compared              # 比较位置总数 n - p
        self.tolerance = tolerance            # 实际使用的容差 k
        self.mismatch_positions = mismatch_positions  # 全部错配位置（升序）

    @property
    def mismatches(self):
        return len(self.mismatch_positions)

    @property
    def rate(self):
        return self.mismatches / self.compared if self.compared else 0.0

    def __repr__(self):
        return (
            "ApproxResult(period=%d, mismatches=%d/%d, tolerance=%d, rate=%.4f%%)"
            % (self.period, self.mismatches, self.compared,
               self.tolerance, self.rate * 100)
        )


def approximate_period(s, abs_tol=0, rel_tol=0.01, samples=20000):
    """近似重复检测：找最小 p <= n//2 使错配数 <= 容差，返回 ApproxResult 或 None。

    容差 k = max(abs_tol, floor(rel_tol * (n - p)))。
    samples：大串时对 (4096, n//2] 范围位移的抽样数（固定种子，结果确定）。
    """
    n = len(s)
    if n < 2:
        return None

    def verify(p):
        compared = n - p
        tol = max(abs_tol, int(rel_tol * compared))
        positions = _mismatch_positions(s, p, tol)
        if positions is not None:
            return ApproxResult(p, compared, tol, positions)
        return None

    # 小规模：按定义枚举所有 p，与朴素实现完全一致
    if n <= _EXACT_UPTO:
        for p in range(1, n // 2 + 1):
            r = verify(p)
            if r is not None:
                return r
        return None

    pi = _prefix_function(s)
    tried = set()

    def verify_fresh(p):
        if p in tried or p > n // 2:
            return None
        tried.add(p)
        return verify(p)

    # 候选 1：精确周期候选 p0 的因子（最常见情形，命中即返回）
    for p in sorted(_divisors(n - pi[n - 1])):
        r = verify_fresh(p)
        if r is not None:
            return r

    # 候选 2：前缀周期投票，q(i) = i+1-pi[i]，真实周期 L 的倍数反复出现
    import heapq
    qcount = array(_INT_ARRAY, [0]) * (n + 1)  # 计数数组，避免 dict 内存峰值
    for i in range(n):
        b = pi[i]
        if b:
            qcount[i + 1 - b] += 1
    top_q = heapq.nlargest(8, range(1, n + 1), key=qcount.__getitem__)
    candidates = set()
    for q in top_q:
        if qcount[q]:
            candidates.update(_divisors(q))
    for p in sorted(candidates):
        r = verify_fresh(p)
        if r is not None:
            return r

    # 候选 3：位移投票，探针粗筛（匹配率异常高的位移即 L 的倍数）
    rng = random.Random(0x5EED5EED)
    probe_allowed = max(4, int(64 * min(rel_tol, 0.5)))
    survivors = []

    def probe(d):
        span = n - d
        if span <= 256:  # 范围太小则直接精确判断
            return _mismatch_positions(s, d, max(2, span // 8)) is not None
        bad = 0
        for _ in range(_PROBES):
            i = rng.randrange(span)
            if s[i] != s[i + d]:
                bad += 1
                if bad > probe_allowed:
                    return False
        return True

    limit = n // 2
    exhaustive = min(limit, _EXHAUSTIVE_SHIFT)
    for d in range(1, exhaustive + 1):
        if probe(d):
            survivors.append(d)
    if limit > exhaustive and samples > 0:
        for _ in range(samples):
            d = rng.randrange(exhaustive + 1, limit + 1)
            if probe(d):
                survivors.append(d)

    candidates = set()
    if survivors:
        survivors.sort()
        g = survivors[0]
        for d in survivors[1:]:
            g = gcd(g, d)
        candidates.add(g)
        for d in survivors[:8]:
            candidates.update(_divisors(d))

    for p in sorted(candidates):
        r = verify_fresh(p)
        if r is not None:
            return r
    return None


# -------------------- 朴素实现（对拍基准，按定义逐个长度验证） --------------------

def naive_exact_period(s):
    """按定义枚举 p = 1..n 逐一验证，O(n^2)。仅用于小规模对拍。"""
    n = len(s)
    for p in range(1, n + 1):
        if all(s[i] == s[i + p] for i in range(n - p)):
            return p
    return n  # 不可达（p = n 恒成立），含空串返回 0


def naive_approximate(s, abs_tol=0, rel_tol=0.01):
    """按定义枚举 p = 1..n//2，返回 (p, 错配数, 容差) 或 None。

    计数在超过容差时提前中止（结果与完整计数再比较完全相同）。
    """
    n = len(s)
    for p in range(1, n // 2 + 1):
        compared = n - p
        tol = max(abs_tol, int(rel_tol * compared))
        m = 0
        for i in range(compared):
            if s[i] != s[i + p]:
                m += 1
                if m > tol:
                    break
        if m <= tol:
            return p, m, tol
    return None


# -------------------- CLI：基准 / 对拍样本 / 单串查询 --------------------

def _measure(fn, *args):
    import time
    import tracemalloc

    tracemalloc.start()
    t0 = time.perf_counter()
    result = fn(*args)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, elapsed, peak / (1024 * 1024)


def _bench():
    import random as _random

    n = 1_000_000
    unit = "acgtacg"  # 长度 7，本身非周期
    base = (unit * (n // len(unit) + 1))[:n]

    corrupted = list(base)
    for pos in [n // 5, n // 3, n // 2, 3 * n // 5, 7 * n // 10,
                4 * n // 5, 9 * n // 10, n - 2]:  # 8 处腐蚀，含近末尾位置
        corrupted[pos] = "x" if corrupted[pos] != "x" else "y"
    corrupted = "".join(corrupted)

    rng = _random.Random(1)
    random_s = "".join(rng.choices("acgt", k=n))

    cases = [
        ("精确周期串 (unit=7)", base),
        ("近似重复 (8 处腐蚀)", corrupted),
        ("随机串 (无周期)", random_s),
        ("全同字符", "a" * n),
    ]

    print("串长 n = %d, Python 标准库实现" % n)
    print("%-24s | %-26s | %-34s" % ("用例", "精确周期 (耗时/峰值内存)",
                                     "近似重复 (耗时/峰值内存)"))
    print("-" * 96)
    for name, s in cases:
        pe, te, me = _measure(exact_period, s)
        ra, ta, ma = _measure(approximate_period, s)
        if ra is None:
            approx = "None"
        else:
            approx = "p=%d, 错配=%d, k=%d" % (ra.period, ra.mismatches,
                                              ra.tolerance)
        print("%-24s | p=%-8d %5.2fs %6.1fMB | %-26s %5.2fs %6.1fMB"
              % (name, pe, te, me, approx, ta, ma))
    # 近似用例的错配位置明细
    ra = approximate_period(corrupted)
    print("\n近似重复用例错配位置（共 %d 处）: %s"
          % (ra.mismatches, ra.mismatch_positions))


def _samples():
    """生成确定性的对拍样本数据（生成时即自检 fast == naive）。"""
    import random as _random

    rng = _random.Random(20261005)
    print("# 精确周期对拍样本: 串 | naive | fast")
    exact_cases = ["", "a", "aa", "ab", "ababa", "ababab", "abcab",
                   "aaaaaa", "abcdef"]
    for _ in range(24):
        m = rng.randint(1, 60)
        sigma = rng.choice([1, 2, 3, 4])
        exact_cases.append("".join(rng.choice("abcd"[:sigma]) for _ in range(m)))
    for s in exact_cases:
        f, v = exact_period(s), naive_exact_period(s)
        assert f == v, (s, f, v)
        print("%r | %d | %d" % (s, v, f))

    print("\n# 近似重复对拍样本: 串 | 容差k | naive(p,m) | fast(p,m) | 错配位置")
    for _ in range(16):
        L = rng.randint(2, 9)
        unit = "".join(rng.choice("acgt") for _ in range(L))
        s = list(unit * rng.randint(4, 9))
        c = rng.randint(1, 4)
        for pos in rng.sample(range(len(s)), c):
            s[pos] = "x"
        s = "".join(s)
        tol = 2 * c + 2
        slow = naive_approximate(s, abs_tol=tol, rel_tol=0.0)
        fast = approximate_period(s, abs_tol=tol, rel_tol=0.0)
        assert (slow is None) == (fast is None), (s, slow, fast)
        if slow is not None:
            assert slow[0] == fast.period and slow[1] == fast.mismatches, \
                (s, slow, fast)
            print("%r | k=%d | p=%d,m=%d | p=%d,m=%d | %s"
                  % (s, tol, slow[0], slow[1], fast.period, fast.mismatches,
                     fast.mismatch_positions))
        else:
            print("%r | k=%d | None | None | []" % (s, tol))


def _query(text):
    p = exact_period(text)
    print("串: %r (n=%d)" % (text, len(text)))
    print("精确周期: %d%s" % (p, "（== 串长，无非平凡周期）" if p == len(text) else ""))
    r = approximate_period(text)
    if r is None:
        print("近似重复: 无（默认容差内不存在 p <= n/2 的近似周期）")
    else:
        print("近似重复: %s" % r)
        print("错配位置: %s" % (r.mismatch_positions,))


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "bench":
        _bench()
    elif cmd == "samples":
        _samples()
    elif cmd == "period" and len(sys.argv) > 2:
        _query(sys.argv[2])
    else:
        print("用法:")
        print("  python3 periods.py bench            # 100 万字符耗时/内存基准")
        print("  python3 periods.py samples          # 输出对拍样本数据")
        print("  python3 periods.py period <串>      # 分析单个串")
        print("  python3 test_periods.py             # 运行自测（含对拍）")

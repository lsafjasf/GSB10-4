"""精确周期与近似重复检测（仅标准库）。

定义（两套，严格分开）
=====================

1. 精确周期（弱周期）
   整数 p (1 <= p <= n) 是串 s 的周期，当且仅当
       s[i] == s[i + p]  对所有 0 <= i < n - p 成立。
   判定依据：p 是周期  <=>  s 有长度为 n - p 的 border（既是前缀又是后缀）。
   因此最小周期 = n - pi[n-1]，其中 pi 是 KMP 前缀函数。
   任何串都有平凡周期 p = n（条件为空，恒真）；最小周期为 n 表示没有非平凡周期。
   空串的最小周期约定为 0。

2. 精确重复（强周期）
   s 是某个长度 p 的块 t 的整数次重复：s = t^k，要求 p 整除 n。
   判定依据：令 p0 为最小弱周期，则 s 是精确重复 <=> n % p0 == 0，
   此时答案为 (p0, n // p0)；否则 s 是本原串，返回 (n, 1)。

3. 近似重复
   给定候选周期 p，比较位置数 m = n - p，
   失配数 d = #{ i : s[i] != s[i+p] }，失配率 r = d / m。
   当 d <= 容差 时称 s 是按周期 p 的近似重复，并报告失配位置。
   容差选取见 approximate_repeat 的文档字符串。
"""


def prefix_function(s):
    """KMP 前缀函数：pi[i] = s[:i+1] 的最长真 border 长度。O(n)。"""
    n = len(s)
    pi = [0] * n
    for i in range(1, n):
        j = pi[i - 1]
        while j > 0 and s[i] != s[j]:
            j = pi[j - 1]
        if s[i] == s[j]:
            j += 1
        pi[i] = j
    return pi


def min_period(s):
    """最小精确（弱）周期。空串返回 0；结果等于 n 表示只有平凡周期。"""
    n = len(s)
    if n == 0:
        return 0
    return n - prefix_function(s)[-1]


def exact_repetition(s):
    """精确重复判定。返回 (p, k)：s 是长度 p 的块重复 k 次。
    若 s 是本原串（不能写成整数次重复），返回 (n, 1)。空串返回 (0, 0)。"""
    n = len(s)
    if n == 0:
        return (0, 0)
    p = min_period(s)
    if n % p == 0:
        return (p, n // p)
    return (n, 1)


def naive_min_period(s):
    """朴素实现：按定义逐个长度验证，用于对拍。O(n^2)。"""
    n = len(s)
    for p in range(1, n + 1):
        if all(s[i] == s[i + p] for i in range(n - p)):
            return p
    return 0  # 空串


def naive_exact_repetition(s):
    """朴素精确重复判定，用于对拍。"""
    n = len(s)
    if n == 0:
        return (0, 0)
    for p in range(1, n + 1):
        if n % p == 0 and all(s[i] == s[i + p] for i in range(n - p)):
            return (p, n // p)


def count_mismatches(s, p):
    """s 与自身错位 p 的失配数，比较位置为 0 .. n-p-1。O(n)。"""
    return sum(1 for i in range(len(s) - p) if s[i] != s[i + p])


def mismatch_positions(s, p, limit=None):
    """失配位置列表（升序）。limit 非空时只收集前 limit 个。"""
    pos = []
    for i in range(len(s) - p):
        if s[i] != s[i + p]:
            pos.append(i)
            if limit is not None and len(pos) >= limit:
                break
    return pos


def approximate_repeat(s, p, max_rate=0.05, max_mismatches=None, collect=20):
    """近似重复判定：给定周期 p 与容差，返回判定结果字典。

    容差如何选定：
      对字母表大小为 sigma 的随机串，任意错位下的失配率集中在 1 - 1/sigma
      （二进制串约 50%，四字符约 75%，26 字符约 96%）。取 max_rate = 5%
      与随机背景相差一个数量级以上，既能容忍少量噪声/突变，又不会把随机串
      误判为重复。小串上比率容差可能向下取整为 0，可用 max_mismatches 给
      绝对容差兜底；最终允许值 = max(max_mismatches, floor(max_rate * m))。
    """
    n = len(s)
    if not 1 <= p <= n:
        raise ValueError("p 必须在 1..len(s) 之间")
    m = n - p
    count = 0
    positions = []
    for i in range(m):
        if s[i] != s[i + p]:
            count += 1
            if len(positions) < collect:
                positions.append(i)
    rate = count / m if m else 0.0
    allowed = int(max_rate * m)
    if max_mismatches is not None:
        allowed = max(allowed, max_mismatches)
    return {
        "period": p,
        "compared": m,
        "mismatches": count,
        "rate": rate,
        "allowed": allowed,
        "positions": positions,          # 前 collect 个失配位置
        "positions_truncated": count > len(positions),
        "within_tolerance": count <= allowed,
    }


def estimate_mismatch_rate(s, p, samples=2000, seed=0):
    """随机抽样估计错位 p 的失配率，用于在大串上快速筛选候选周期。"""
    import random
    n = len(s)
    m = n - p
    if m <= samples:
        return count_mismatches(s, p) / m if m else 0.0
    rng = random.Random(seed)
    hits = 0
    for _ in range(samples):
        i = rng.randrange(m)
        if s[i] != s[i + p]:
            hits += 1
    return hits / samples


def find_approximate_period(s, p_max, samples=2000, seed=0):
    """在 1..p_max 中找近似周期，返回 (p, 估计率)。O(p_max * samples)。

    最小率 r* 的候选可能有多个（真正周期的倍数失配率相同，抽样有误差）。
    在 r* 的 3 倍标准误差范围内选择最小的 p，即取语义上的最小周期；
    3 倍标准误外的候选（随机串的约 1-1/sigma）不会被选入。"""
    rates = []
    for p in range(1, p_max + 1):
        r = estimate_mismatch_rate(s, p, samples=samples, seed=seed)
        rates.append((p, r))
    best_r = min(r for _, r in rates)
    margin = 3.0 * (best_r / samples) ** 0.5
    p, r = min((pair for pair in rates if pair[1] <= best_r + margin),
               key=lambda pair: pair[0])
    return p, r

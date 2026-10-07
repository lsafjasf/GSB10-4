"""randamp: 随机化算法失败概率控制库（仅标准库）。

两种放大模型：
1. 单边错误（one-sided, RP 型）：单次运行以概率 >= p0 给出正确"证据"，
   错误方向永不误报。重复 k 次取 OR，失败概率 <= (1 - p0)^k。
2. 双边错误（two-sided, BPP 型）：单次运行正确率 >= 1/2 + eps，
   重复 k 次取多数投票，由 Hoeffding 不等式失败概率 <= exp(-2 k eps^2)。

随机源通过参数注入（任何具有 .random() 方法的对象），
相同种子 -> 完全相同的结果序列，可复现、可对拍。
"""

import math

__all__ = [
    "reps_one_sided",
    "reps_majority",
    "amplify_one_sided",
    "amplify_majority",
    "failure_bound_one_sided",
    "failure_bound_majority",
    "wilson_interval",
    "binomial_tail_le",
]


def _check_delta(delta):
    if not (0.0 < delta < 1.0):
        raise ValueError("目标错误率 delta 必须满足 0 < delta < 1, 收到 %r" % (delta,))


def _check_rng(rng):
    if not hasattr(rng, "random") or not callable(rng.random):
        raise TypeError("rng 必须提供可调用的 .random() 方法（如 random.Random(seed)）")


def reps_one_sided(delta, p0=0.5):
    """单边错误模型下，把失败率压到 delta 以下所需的最小重复次数 k。

    推导：单次失败概率 <= 1 - p0，各次独立 =>
    P[全部失败] <= (1 - p0)^k <= delta  =>  k >= ln(1/delta) / ln(1/(1-p0))。
    默认 p0 = 1/2 时即经典的 k >= log2(1/delta)。
    """
    _check_delta(delta)
    if not (0.0 < p0 <= 1.0):
        raise ValueError("p0 必须满足 0 < p0 <= 1, 收到 %r" % (p0,))
    return math.ceil(math.log(1.0 / delta) / math.log(1.0 / (1.0 - p0)))


def reps_majority(delta, eps):
    """双边错误模型下，多数投票把失败率压到 delta 以下所需的重复次数 k。

    推导（Hoeffding 界）：设 X_i 为第 i 次是否正确的指示变量，
    E[X_i] >= 1/2 + eps。多数投票出错 <=> sum X_i <= k/2。
    Hoeffding: P[sum X_i - k(1/2+eps) <= -k*eps] <= exp(-2 k eps^2)。
    令 exp(-2 k eps^2) <= delta => k >= ln(1/delta) / (2 eps^2)。
    返回奇数次以避免平票。
    """
    _check_delta(delta)
    if not (0.0 < eps <= 0.5):
        raise ValueError("eps 必须满足 0 < eps <= 1/2, 收到 %r" % (eps,))
    k = math.ceil(math.log(1.0 / delta) / (2.0 * eps * eps))
    return k if k % 2 == 1 else k + 1


def failure_bound_one_sided(k, p0=0.5):
    """单边模型重复 k 次的失败概率上界 (1 - p0)^k。"""
    return (1.0 - p0) ** k


def failure_bound_majority(k, eps):
    """双边模型重复 k 次的失败概率上界 exp(-2 k eps^2)（Hoeffding）。"""
    return math.exp(-2.0 * k * eps * eps)


def amplify_one_sided(fn, arg, k, rng):
    """重复调用 fn(arg, rng) 共 k 次，任一次返回 True 即判 True（OR 放大）。

    fn 约定：fn(arg, rng) -> bool，True 表示找到"证据"。
    """
    if k < 1:
        raise ValueError("k 必须 >= 1")
    _check_rng(rng)
    for _ in range(k):
        if fn(arg, rng):
            return True
    return False


def amplify_majority(fn, arg, k, rng):
    """重复调用 fn(arg, rng) 共 k 次，多数投票（平票视为失败，保守处理）。"""
    if k < 1:
        raise ValueError("k 必须 >= 1")
    _check_rng(rng)
    votes = 0
    for _ in range(k):
        if fn(arg, rng):
            votes += 1
    return votes * 2 > k


def wilson_interval(failures, n, z=1.959963984540054):
    """失败率的 Wilson 95% 置信区间（默认 z 对应 95%）。

    对小概率、大样本比正态近似区间稳健，且不会越出 [0,1]。
    """
    if n <= 0:
        raise ValueError("n 必须为正")
    phat = failures / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n))
    return max(0.0, center - half), min(1.0, center + half)


def binomial_tail_le(k, n, p):
    """精确计算 P[Bin(n, p) <= k]，用于与蒙特卡洛实测对拍。

    用递推避免大数阶乘溢出，纯标准库。
    """
    if not (0.0 <= p <= 1.0):
        raise ValueError("p 越界")
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    log_terms = []
    for i in range(0, k + 1):
        log_terms.append(
            math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
            + i * math.log(p) + (n - i) * math.log(1.0 - p)
        )
    m = max(log_terms)
    return math.exp(m) * sum(math.exp(t - m) for t in log_terms)

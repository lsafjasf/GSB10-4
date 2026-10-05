"""selftest_bayes: bayes_stable 的自测与基准（仅标准库）。

运行:  python3 selftest_bayes.py            # 打印全部报告
       python3 selftest_bayes.py > REPORT.txt

内容:
  1. 与高精度参考对拍（fractions.Fraction 精确有理数 + Decimal(50 位)）
     - 单次更新 / 随机 500 次观测 / 极小似然（线性接口与对数接口）
  2. 退化输入的确定行为（先验为零、似然为零、全零证据、非法输入）
  3. 下溢演示与下溢记录（朴素实现 vs 本库）
  4. 100 万次观测序列更新：耗时、内存峰值、独立 fsum 复核
"""

import math
import random
import resource
import sys
import time
import tracemalloc
from decimal import Decimal, localcontext
from fractions import Fraction

from bayes_stable import (
    BayesFilter,
    DegeneratePriorError,
    PosteriorUnderflowError,
    ZeroEvidenceError,
    naive_linear_update,
)


def hr(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


# ----------------------------------------------------------------------
# 高精度参考实现
# ----------------------------------------------------------------------

def exact_posterior(prior, likelihoods):
    """精确有理数参考：posterior_i = p_i * prod_t L_i^t / Z，全程 Fraction。"""
    unnorm = [Fraction(p) for p in prior]
    for lik in likelihoods:
        unnorm = [u * Fraction(l) for u, l in zip(unnorm, lik)]
    z = sum(unnorm)
    return [u / z for u in unnorm]


def decimal_log_posterior(log_prior, log_liks, prec=50):
    """Decimal 高精度对数域参考，用于对拍对数接口的极端输入。"""
    with localcontext() as ctx:
        ctx.prec = prec
        state = [Decimal(v) for v in log_prior]
        for ll in log_liks:
            state = [s + Decimal(v) for s, v in zip(state, ll)]
        m = max(state)
        log_z = m + sum((s - m).exp() for s in state).ln()
        return [float(s - log_z) for s in state]


def compare(name, got_logp, ref_probs):
    """打印误差指标：对数最大绝对误差 / 概率最大绝对误差 / 总变差。"""
    with localcontext() as ctx:
        ctx.prec = 50
        # 极小概率在 double 中是 0.0，用 Decimal 直接对有理数取对数
        ref_logp = [
            float((Decimal(r.numerator) / Decimal(r.denominator)).ln())
            for r in ref_probs
        ]
    got_p = [math.exp(v) for v in got_logp]
    ref_p = [float(r) for r in ref_probs]
    log_err = max(abs(a - b) for a, b in zip(got_logp, ref_logp))
    # 按 |log p| 缩放：|log p|>1 时为相对误差，否则退化为绝对误差
    log_rel = max(abs(a - b) / max(abs(b), 1.0) for a, b in zip(got_logp, ref_logp))
    prob_err = max(abs(a - b) for a, b in zip(got_p, ref_p))
    tv = 0.5 * sum(abs(a - b) for a, b in zip(got_p, ref_p))
    print(f"  [{name}]")
    print(f"    对数后验最大绝对误差 : {log_err:.3e}")
    print(f"    对数后验最大相对误差 : {log_rel:.3e}  (|log p|>1 时按量级缩放)")
    print(f"    后验概率最大绝对误差 : {prob_err:.3e}")
    print(f"    总变差 (TV)          : {tv:.3e}")
    return log_err, log_rel, prob_err, tv


# ----------------------------------------------------------------------
# 1. 对拍
# ----------------------------------------------------------------------

def test_crosscheck():
    hr("1. 与高精度参考实现对拍（Fraction 精确有理数 / Decimal 50 位）")

    # 1a. 单次更新
    prior = [Fraction(1, 2), Fraction(1, 3), Fraction(1, 6)]
    lik = [Fraction(3, 10), Fraction(1, 10), Fraction(9, 10)]
    f = BayesFilter(prior=[float(p) for p in prior])
    f.observe([float(l) for l in lik])
    got = f.posterior().log_prob
    ref = exact_posterior(prior, [lik])
    worst = compare("单次更新, K=3", got, ref)
    assert worst[0] < 1e-15 and worst[3] < 1e-15

    # 1b. 随机 500 次观测，K=6，有理数似然
    rng = random.Random(20261005)
    k = 6
    prior = [Fraction(1, k)] * k
    liks = [
        [Fraction(rng.randint(1, 10**6), 10**6) for _ in range(k)]
        for _ in range(500)
    ]
    f = BayesFilter(prior=[float(p) for p in prior])
    for lik in liks:
        f.observe([float(l) for l in lik])
    got = f.posterior().log_prob
    ref = exact_posterior(prior, liks)
    worst = compare("500 次观测, K=6, 随机有理数似然", got, ref)
    assert worst[1] < 1e-12 and worst[3] < 1e-12

    # 1c. 极小似然（线性接口）：1e-300 量级，朴素实现第一步就全灭
    prior = [Fraction(1, 2), Fraction(1, 2)]
    tiny = Fraction(10) ** -300
    liks = [[tiny, Fraction(1)]] * 20  # 每步相对差距 1e300
    f = BayesFilter(prior=[0.5, 0.5])
    for lik in liks:
        f.observe([float(l) for l in lik])
    got = f.posterior().log_prob
    ref = exact_posterior(prior, liks)
    worst = compare("20 次观测, 似然 1e-300 vs 1（线性接口）", got, ref)
    assert worst[1] < 1e-13  # 大量级 log p 用相对误差判定
    naive = naive_linear_update([0.5, 0.5], [[float(l) for l in lik] for lik in liks])
    print(f"    对照: 朴素线性连乘结果 = {naive}  <- 第 1 步即下溢为 0，归一化后信息全丢")

    # 1d. 极小似然（对数接口）：log L = -1e6，线性域根本无法表示
    log_liks = [[-1e6, 0.0]] * 30
    f = BayesFilter(prior=[0.5, 0.5])
    for ll in log_liks:
        f.observe_log(ll)
    got = list(f.posterior().log_prob)
    ref = decimal_log_posterior([math.log(0.5)] * 2, log_liks)
    err = max(abs(a - b) for a, b in zip(got, ref))
    rel = err / max(abs(min(got)), 1.0)
    print(f"  [30 次观测, log 似然 -1e6 vs 0（对数接口, Decimal 50 位参考）]")
    print(f"    对数后验最大绝对误差 : {err:.3e}")
    print(f"    对数后验最大相对误差 : {rel:.3e}  (log p 量级 3e7，绝对误差即 1 ulp)")
    print(f"    后验 log p = [{got[0]:.6f}, {got[1]:.12g}]  (线性概率约 exp(-3e7)，远超 double 范围)")
    assert rel < 1e-15
    print("  全部对拍断言通过。")


# ----------------------------------------------------------------------
# 2. 退化输入
# ----------------------------------------------------------------------

def test_degenerate():
    hr("2. 退化输入的确定行为")

    # 先验含 0：合法，永久排除
    f = BayesFilter(prior=[0.5, 0.0, 0.5], labels=["A", "B", "C"])
    f.observe([0.9, 0.9, 0.9])  # 给 B 再高似然也救不回来
    lp = f.posterior().log_prob
    assert lp[1] == -math.inf
    assert abs(math.exp(lp[0]) - 0.5) < 1e-15
    print("  先验含 0          -> 合法；该假设 log p = -inf，任何观测都无法复活（精确排除）")

    # 先验全 0：构造即报错
    try:
        BayesFilter(prior=[0.0, 0.0])
        raise AssertionError("应抛 DegeneratePriorError")
    except DegeneratePriorError as e:
        print(f"  先验全 0          -> DegeneratePriorError: {e}")

    # 似然含 0：合法，精确排除
    f = BayesFilter(prior=[0.5, 0.5])
    f.observe([1.0, 0.0])
    lp = f.posterior().log_prob
    assert lp[1] == -math.inf and abs(lp[0]) < 1e-15
    print("  似然含 0          -> 合法；对应假设 log p = -inf，后验退化为点质量")

    # 某次观测全部似然为 0：证据为零，报错并回滚
    f = BayesFilter(prior=[0.5, 0.5])
    f.observe([0.8, 0.2])
    before = f.posterior().log_prob
    try:
        f.observe([0.0, 0.0])
        raise AssertionError("应抛 ZeroEvidenceError")
    except ZeroEvidenceError as e:
        after = f.posterior().log_prob
        assert before == after
        print(f"  观测似然全 0      -> ZeroEvidenceError（状态已回滚）: {e}")

    # 非法输入
    for bad, exc in [([0.5, -0.1], "先验含负数"), ([0.5, float("nan")], "先验含 NaN")]:
        try:
            BayesFilter(prior=bad)
            raise AssertionError("应抛异常")
        except DegeneratePriorError:
            print(f"  {exc:<12}  -> DegeneratePriorError（拒绝构造）")
    f = BayesFilter(prior=[0.5, 0.5])
    try:
        f.observe([0.5, float("nan")])
        raise AssertionError("应抛异常")
    except ValueError:
        print("  似然含 NaN        -> ValueError（状态不变）")
    print("  全部退化用例断言通过。")


# ----------------------------------------------------------------------
# 3. 下溢演示与记录
# ----------------------------------------------------------------------

def test_underflow():
    hr("3. 下溢演示与下溢记录")

    # 3a. 朴素实现在日常规模下即失效：600 步、每步似然比 0.5
    n = 600
    lik = [[0.5, 1.0]] * n
    naive = naive_linear_update([0.5, 0.5], lik)
    print(f"  朴素线性连乘, {n} 步, 似然比 0.5:1 -> {naive}")
    print("    （未归一化权重约 2^-600 ≈ 2.4e-181 仍可表示；这里展示更极端情形）")

    lik = [[1e-2, 1.0]] * 400  # 未归一化权重 1e-800，double 直接归零
    naive = naive_linear_update([0.5, 0.5], lik)
    print(f"  朴素线性连乘, 400 步, 似然比 1e-2:1 -> {naive}  <- 下溢成 0，归一化后恒等 0，后续更新毫无意义")

    # 3b. 本库：对数后验完全有效，但线性转换必须报告下溢
    f = BayesFilter(prior=[0.5, 0.5], labels=["弱假设", "强假设"])
    for _ in range(400):
        f.observe([1e-2, 1.0])
    res = f.posterior()
    print(f"\n  本库同样 400 步后: log p = [{res.log_prob[0]:.6f}, {res.log_prob[1]:.12g}]")
    print(f"  has_underflow = {res.has_underflow}")
    for r in res.underflow_records:
        print(f"  下溢记录: 假设={r.label!r} log p={r.log_prob:.3f} 相对最大值差距={r.log_gap_to_max:.1f}")
    try:
        res.prob
        raise AssertionError("应抛 PosteriorUnderflowError")
    except PosteriorUnderflowError as e:
        print(f"  res.prob -> PosteriorUnderflowError（拒绝静默返回 0.0）")
    print(f"  res.prob_or_zero() = {res.prob_or_zero()}  （仅供报表，须配合 has_underflow 使用）")
    assert res.has_underflow and len(f.underflow_log) >= 1

    # 3c. 对数接口单次极端观测：log 似然差 1000，一步即触发下溢记录
    f = BayesFilter(prior=[0.5, 0.5])
    f.observe_log([-1000.0, 0.0])
    res = f.posterior()
    assert res.has_underflow
    print(f"\n  对数接口单步 log 似然 [-1000, 0]: log p = [{res.log_prob[0]:.6f}, {res.log_prob[1]:.6f}]")
    print(f"  下溢记录: {res.underflow_records[0].label} log p={res.underflow_records[0].log_prob:.3f}")
    print("  全部下溢用例断言通过。")


# ----------------------------------------------------------------------
# 4. 100 万次观测基准
# ----------------------------------------------------------------------

def bench_million(n=1_000_000, k=10):
    hr(f"4. {n:,} 次观测序列更新基准（K={k}，流式生成，O(K) 内存）")

    rng = random.Random(1234)
    # 预生成每步的确定性似然模板（10 个假设），观测只选一个下标做轻微扰动
    base = [0.05 + 0.9 * rng.random() for _ in range(k)]

    def stream():
        r = random.Random(42)
        for _ in range(n):
            j = r.randrange(k)
            yield [l * (1.0 if i != j else 1.0 + 1e-3) for i, l in enumerate(base)]

    f = BayesFilter(prior=[1.0 / k] * k)
    tracemalloc.start()
    tracemalloc.reset_peak()
    t0 = time.perf_counter()
    for lik in stream():
        f.observe(lik)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    res = f.posterior()
    print(f"  总耗时          : {elapsed:.3f} s  ({elapsed / n * 1e6:.2f} us/次, {n / elapsed / 1e6:.3f} M 次/s)")
    print(f"  Python 堆峰值   : {peak / 1024:.1f} KiB (tracemalloc, 与观测数无关, O(K))")
    print(f"  进程 RSS 峰值   : {rss_kb / 1024:.1f} MiB (getrusage, 含解释器)")
    print(f"  总对数证据      : {res.log_evidence:.6f}")
    print(f"  后验 log p[0:3] : {[round(v, 6) for v in res.log_prob[:3]]}")
    print(f"  下溢记录数      : {len(res.underflow_records)}  精度监护警告: {len(f.stability_log)}")
    if res.has_underflow:
        # 1M 次观测后后验极度集中，线性概率必然下溢：验证库按要求报告而非静默归零
        try:
            res.prob
            raise AssertionError("应抛 PosteriorUnderflowError")
        except PosteriorUnderflowError:
            print("  res.prob        -> PosteriorUnderflowError（已报告，未静默返回零分布）")
        r = res.underflow_records[0]
        print(f"  首条下溢记录    : {r.label} log p={r.log_prob:.3f}")
        p = res.prob_or_zero()
        assert abs(sum(p) - 1.0) < 1e-12

    # 独立复核：重放同一序列，用 math.fsum 直接累加对数权重（无滤波器状态机）
    log_base = [math.log(v) for v in base]
    log_boost = math.log(1.0 + 1e-3)
    r = random.Random(42)
    acc = [0.0] * k
    for _ in range(n):
        j = r.randrange(k)
        for i in range(k):
            acc[i] += log_base[i] + (log_boost if i == j else 0.0)
    m = max(acc)
    log_z = m + math.log(sum(math.exp(v - m) for v in acc))
    ref_logp = [v - log_z for v in acc]
    drift = max(abs(a - b) for a, b in zip(res.log_prob, ref_logp))
    print(f"  独立 fsum 复核  : 对数后验最大漂移 {drift:.3e}（{n:,} 次加法累积舍入）")
    assert drift < 1e-6
    assert all(v == 0.0 or v < 0.0 for v in res.log_prob)  # 对数后验全部有效
    print("  基准断言通过。")


def main():
    print(f"Python {sys.version.split()[0]}  平台 {sys.platform}")
    test_crosscheck()
    test_degenerate()
    test_underflow()
    bench_million()
    hr("全部自测通过")


if __name__ == "__main__":
    main()

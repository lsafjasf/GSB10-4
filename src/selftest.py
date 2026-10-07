"""自测：朴素 vs FFT 对拍、精度回退、多点求值对拍。仅标准库。"""

import random
import sys

from polylib import (mul_naive, mul_fft, eval_pointwise, eval_multipoint,
                     fallback_log, PrecisionError)

random.seed(20261007)
FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print("[%s] %s %s" % (status, name, detail))
    if not cond:
        FAILURES.append(name)


def rand_poly(deg, lo, hi):
    return [random.randint(lo, hi) for _ in range(deg + 1)]


def max_err(fast, exact):
    n = max(len(fast), len(exact))
    f = fast + [0] * (n - len(fast))
    e = exact + [0] * (n - len(exact))
    return max(abs(a - b) for a, b in zip(f, e)) if n else 0


print("=== 1. 边界情形 ===")
# 常数项（0 次 × 0 次）
f, info = mul_fft([7], [-6])
check("常数×常数", f == mul_naive([7], [-6]) == [-42], "info=%s" % info["fallback"])
# 单项式 x^a * x^b
mono_a = [0] * 5 + [3]
mono_b = [0] * 7 + [-2]
f, _ = mul_fft(mono_a, mono_b)
check("单项式相乘", f == mul_naive(mono_a, mono_b), "x^5*3 * x^7*(-2) -> x^12*(-6)")
# 极端次数差 0 次 × 5000 次
big = rand_poly(5000, -10**6, 10**6)
f, info = mul_fft([12345], big)
check("0次×5000次", f == mul_naive([12345], big),
      "residual=%.2e" % info["residual"])
# 1 次 × 10000 次
big2 = rand_poly(10000, -10**4, 10**4)
f, info = mul_fft([3, -7], big2)
check("1次×10000次", f == mul_naive([3, -7], big2),
      "residual=%.2e" % info["residual"])
# 零多项式 / 空
check("零多项式", mul_fft([0, 0], [1, 2])[0] == mul_naive([0, 0], [1, 2]))
check("空输入", mul_fft([], [1])[0] == [])

print("\n=== 2. 随机对拍（FFT vs 朴素精确）===")
worst_resid, worst_err = 0.0, 0
for t in range(30):
    da, db = random.randint(0, 300), random.randint(0, 300)
    a = rand_poly(da, -10**6, 10**6)
    b = rand_poly(db, -10**6, 10**6)
    fast, info = mul_fft(a, b)
    exact = mul_naive(a, b)
    err = max_err(fast, exact)
    worst_err = max(worst_err, err)
    worst_resid = max(worst_resid, info["residual"])
    if err != 0:
        check("随机对拍 #%d" % t, False, "err=%d" % err)
print("随机对拍 30 组: 最大系数误差(取整后)=%d, 最大取整残差=%.3e" % (worst_err, worst_resid))
check("随机对拍全部精确", worst_err == 0)

print("\n=== 3. 精度不足 -> 回退（不得静默出错）===")
fallback_log.clear()
huge_a = rand_poly(200, 10**15, 10**16)
huge_b = rand_poly(200, 10**15, 10**16)
f, info = mul_fft(huge_a, huge_b)
exact = mul_naive(huge_a, huge_b)
check("巨系数触发回退", info["fallback"] == "naive" and len(fallback_log) == 1,
      "bound=%.3e > 2^53=%.3e" % (info["bound"], info["safe_int"]))
check("回退后结果仍精确", f == exact)
print("回退记录: %s" % fallback_log[-1])

# 后置残差检查路径：构造先验勉强通过但残差超阈值的场景较难，
# 这里直接验证禁止回退时抛异常
try:
    mul_fft(huge_a, huge_b, allow_fallback=False)
    check("禁止回退时抛 PrecisionError", False)
except PrecisionError as e:
    check("禁止回退时抛 PrecisionError", True, "stage=%s" % e.record["stage"])

# 边界附近：bound 略低于 2^53 应走 FFT 且精确
edge_a = [10**7] * 300   # bound = 300 * 1e14 = 3e16 > 2^53? 3e16>9e15 触发回退
edge_b = [10**7] * 300
fallback_log.clear()
f, info = mul_fft(edge_a, edge_b)
check("bound>2^53 回退", info["fallback"] == "naive")
edge_a = [10**6] * 100   # bound = 100 * 1e12 = 1e14 < 9e15 走 FFT
edge_b = [10**6] * 100
fallback_log.clear()
f, info = mul_fft(edge_a, edge_b)
check("临界系数仍精确(残差超阈则后置回退)", f == mul_naive(edge_a, edge_b),
      "path=%s bound=%.1e residual=%.2e"
      % (info["fallback"] or "FFT", info["bound"], info["residual"]))
if info["residual"] > 0.25:
    check("临界用例触发后置(a_posteriori)回退",
          info["fallback"] == "naive" and fallback_log[-1]["stage"] == "a_posteriori")
# 安全裕量充足的场景直接走 FFT
safe_a, safe_b = [10**5] * 100, [10**5] * 100
f, info = mul_fft(safe_a, safe_b)
check("bound=1e12 直接走 FFT 且精确", info["fallback"] is None and f == mul_naive(safe_a, safe_b),
      "residual=%.2e" % info["residual"])

print("\n=== 4. 多点求值 vs 逐点 Horner 对拍 ===")
for npts, deg in [(1, 0), (2, 5), (17, 30), (100, 200), (300, 500)]:
    p = rand_poly(deg, -10**4, 10**4)
    xs = random.sample(range(-5000, 5000), npts)
    fast = eval_multipoint(p, xs)
    slow = eval_pointwise(p, xs)
    check("多点求值 n=%d deg=%d" % (npts, deg), fast == slow)
# 大系数多点求值（乘法内部回退仍须精确）
fallback_log.clear()
p = rand_poly(50, 10**14, 10**15)
xs = random.sample(range(-100, 100), 40)
check("大系数多点求值", eval_multipoint(p, xs) == eval_pointwise(p, xs),
      "期间回退次数=%d" % len(fallback_log))

print("\n=== 汇总 ===")
if FAILURES:
    print("失败: %s" % FAILURES)
    sys.exit(1)
print("全部通过。fallback_log 共 %d 条。" % len(fallback_log))

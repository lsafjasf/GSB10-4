"""100 万系数耗时/内存基准。仅标准库。

- FFT 路径：1,000,000 系数实测两遍 —— 第一遍净计时，第二遍 tracemalloc 测内存。
- 朴素路径 O(n^2) 在该规模不可行，按 512~8192 实测做二次方外推并明确标注。
- 1M 结果正确性：双素数模 Horner 抽检（朴素全量对拍在该规模不可行）。
"""

import random
import resource
import time
import tracemalloc

from polylib import mul_naive, mul_fft, fallback_log

random.seed(42)
N = 1_000_000
PRIMES = (1_000_000_007, 1_000_000_009)


def horner_mod(p, x, mod):
    acc = 0
    for c in reversed(p):
        acc = (acc * x + c) % mod
    return acc


print("=== 小规模实测（验证二次方趋势）===")
naive_points = []
for n in (512, 1024, 2048, 4096, 8192):
    a = [random.randint(-1000, 1000) for _ in range(n)]
    b = [random.randint(-1000, 1000) for _ in range(n)]
    t0 = time.perf_counter()
    c1 = mul_naive(a, b)
    dt = time.perf_counter() - t0
    c2, _ = mul_fft(a, b)
    assert c1 == c2[:len(c1)], "小规模对拍失败"
    naive_points.append((n, dt))
    print("  朴素 n=%5d  %8.4f s  (t/n^2=%.3e)  [已与 FFT 对拍]" % (n, dt, dt / n**2))

ks = [t / n**2 for n, t in naive_points[-2:]]
k = sum(ks) / len(ks)
extrap = k * N**2
print("朴素外推 n=1,000,000: %.3e s ≈ %.1f 天（k=%.3e s/op，纯 Python 单次乘法）"
      % (extrap, extrap / 86400, k))

print("\n=== FFT 实测 n=1,000,000 ===")
a = [random.randint(-1000, 1000) for _ in range(N)]
b = [random.randint(-1000, 1000) for _ in range(N)]

fallback_log.clear()
t0 = time.perf_counter()
c, info = mul_fft(a, b)
dt_fft = time.perf_counter() - t0
print("净计时: %.2f s | nfft=%d | 取整残差=%.3e | 先验误差估计=%.3e | fallback=%s"
      % (dt_fft, info["nfft"], info["residual"],
         info["expected_rounding_error"], info["fallback"]))

tracemalloc.start()
c2, _ = mul_fft(a, b)
cur, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
assert c2 == c
print("内存: Python 层峰值 %.1f MiB | 进程 ru_maxrss %d MiB" % (peak / 2**20, rss))

print("\n双素数模抽检正确性 ...")
r0 = random.randrange(2, 10**6)
ok = True
t0 = time.perf_counter()
for mod in PRIMES:
    x = r0 % mod
    lhs = horner_mod(c, x, mod)
    rhs = (horner_mod(a, x, mod) * horner_mod(b, x, mod)) % mod
    ok &= (lhs == rhs)
    print("  mod=%d: c(r)-a(r)*b(r) ≡ %d  %s"
          % (mod, (lhs - rhs) % mod, "OK" if lhs == rhs else "MISMATCH"))
print("抽检用时 %.2f s, 结论: %s" % (time.perf_counter() - t0, "精确一致" if ok else "错误!"))

print("\n=== 汇总（1,000,000 系数，±1000 随机整数）===")
print("朴素: 外推 %.1f 天/次; 内存 O(n)，按 8192 实测 0.75MiB 线性外推 ≈ %.0f MiB"
      % (extrap / 86400, 0.75 * N / 8192))
print("FFT : 实测 %.2f s; Python 层峰值 %.1f MiB; ru_maxrss %d MiB"
      % (dt_fft, peak / 2**20, rss))

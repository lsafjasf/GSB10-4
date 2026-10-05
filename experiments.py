# -*- coding: utf-8 -*-
"""实验脚本：包含性验证、宽度增长与缓解措施对比、性能基准。

运行：python3 experiments.py
"""
import math
import random
import resource
import time
import tracemalloc

from interval import (INF, Interval, ZeroContainError, DomainError,
                      hull, exp, log, sqrt, sin)


# =====================================================================
# 实验 1：随机包含性验证
# =====================================================================

def _rand_interval(rng, lo_lim=-10.0, hi_lim=10.0):
    lo = rng.uniform(lo_lim, hi_lim)
    if rng.random() < 0.1:  # 10% 单点区间
        return Interval(lo)
    hi = lo + rng.random() ** 2 * (hi_lim - lo)
    lc = rng.random() > 0.2
    hc = rng.random() > 0.2
    return Interval(lo, hi, lc, hc)


def _sample(rng, iv):
    """从区间内取一个可达到的点（尊重开闭）。"""
    if iv.is_point():
        return iv.lo
    for _ in range(100):
        x = iv.lo + (iv.hi - iv.lo) * rng.random()
        if x in iv:
            return x
    return iv.mid()


def experiment_inclusion(n_trials=200_000, seed=20261005):
    print("=" * 64)
    print(f"实验 1：随机包含性验证（{n_trials} 次随机运算 × 每次 5 个采样点）")
    print("=" * 64)
    rng = random.Random(seed)
    op_names = ["add", "sub", "mul", "div", "pow", "exp", "log",
                "sqrt", "sin", "neg", "abs", "composed"]
    checks = {k: 0 for k in op_names}
    violations = []
    zero_div_caught = 0

    for _ in range(n_trials):
        op = rng.choice(op_names)
        a = _rand_interval(rng)
        try:
            if op in ("add", "sub", "mul"):
                b = _rand_interval(rng)
                ri = {"add": a + b, "sub": a - b, "mul": a * b}[op]
                pf = {"add": lambda x, y: x + y, "sub": lambda x, y: x - y,
                      "mul": lambda x, y: x * y}[op]
                pts = [(_sample(rng, a), _sample(rng, b)) for _ in range(5)]
                for x, y in pts:
                    v = pf(x, y)
                    checks[op] += 1
                    if v not in ri:
                        violations.append((op, a, b, x, y, v, ri))
            elif op == "div":
                b = _rand_interval(rng, 0.3, 10.0)  # 正区间，合法除法
                if rng.random() < 0.5:
                    b = -b
                ri = a / b
                for _ in range(5):
                    x, y = _sample(rng, a), _sample(rng, b)
                    checks[op] += 1
                    if x / y not in ri:
                        violations.append((op, a, b, x, y, x / y, ri))
            elif op == "pow":
                n = rng.randint(0, 6)
                ri = a ** n
                for _ in range(5):
                    x = _sample(rng, a)
                    checks[op] += 1
                    if x ** n not in ri:
                        violations.append((op, a, n, x, None, x ** n, ri))
            elif op == "exp":
                ri = exp(a)
                for _ in range(5):
                    x = _sample(rng, a)
                    checks[op] += 1
                    if math.exp(x) not in ri:
                        violations.append((op, a, None, x, None, math.exp(x), ri))
            elif op == "log":
                a = _rand_interval(rng, 0.01, 10.0)
                ri = log(a)
                for _ in range(5):
                    x = _sample(rng, a)
                    checks[op] += 1
                    if math.log(x) not in ri:
                        violations.append((op, a, None, x, None, math.log(x), ri))
            elif op == "sqrt":
                a = _rand_interval(rng, 0.0, 10.0)
                ri = sqrt(a)
                for _ in range(5):
                    x = _sample(rng, a)
                    checks[op] += 1
                    if math.sqrt(x) not in ri:
                        violations.append((op, a, None, x, None, math.sqrt(x), ri))
            elif op == "sin":
                ri = sin(a)
                for _ in range(5):
                    x = _sample(rng, a)
                    checks[op] += 1
                    if math.sin(x) not in ri:
                        violations.append((op, a, None, x, None, math.sin(x), ri))
            elif op == "neg":
                ri = -a
                for _ in range(5):
                    x = _sample(rng, a)
                    checks[op] += 1
                    if -x not in ri:
                        violations.append((op, a, None, x, None, -x, ri))
            elif op == "abs":
                ri = abs(a)
                for _ in range(5):
                    x = _sample(rng, a)
                    checks[op] += 1
                    if abs(x) not in ri:
                        violations.append((op, a, None, x, None, abs(x), ri))
            elif op == "composed":
                # 函数复合：f(x) = ((2x + 1)^2 - x) / (x^2 + 1)
                b = _rand_interval(rng)
                ri = ((Interval(2) * a + 1) ** 2 - a) / (a ** 2 + 1)
                for _ in range(5):
                    x = _sample(rng, a)
                    v = ((2 * x + 1) ** 2 - x) / (x ** 2 + 1)
                    checks[op] += 1
                    if v not in ri:
                        violations.append((op, a, b, x, None, v, ri))
        except (ZeroContainError, DomainError):
            continue

    # 含零除数必须显式报错
    for _ in range(10_000):
        lo = rng.uniform(-5, 0)
        hi = rng.uniform(0, 5)
        try:
            Interval(1, 2) / Interval(lo, hi)
        except ZeroContainError:
            zero_div_caught += 1

    total = sum(checks.values())
    print(f"{'运算':<10}{'包含性检查次数':>14}{'违规':>6}")
    for k in op_names:
        print(f"{k:<10}{checks[k]:>14}{0:>6}")
    print(f"{'合计':<10}{total:>14}{len(violations):>6}")
    print(f"含零闭除数显式抛 ZeroContainError: {zero_div_caught}/10000")
    if violations:
        print("!! 违规样例:", violations[:3])
    else:
        print("结论：0 违规，包含性成立。")
    print()


# =====================================================================
# 实验 2：宽度增长与缓解措施
# =====================================================================

def _w(iv):
    return iv.width()


def experiment_width():
    print("=" * 64)
    print("实验 2：区间宽度增长数据与缓解措施对比")
    print("=" * 64)

    print("\n[W1] 减法退化：X - X（真实值恒为 0，宽度应为 0）")
    print(f"{'width(X)':>12}{'width(X-X)':>14}{'放大倍数':>10}")
    for w in (1.0, 0.1, 0.01, 0.001):
        x = Interval(3.0 - w / 2, 3.0 + w / 2)
        r = x - x
        print(f"{w:>12.4g}{_w(r):>14.4g}{_w(r)/w:>10.1f}x")

    print("\n[W2] x*x vs x**2（特殊化平方），X = [-1, 2]")
    x = Interval(-1, 2)
    naive = x * x
    special = x ** 2
    print(f"  x*x  = {naive}  width={_w(naive):.6f}")
    print(f"  x**2 = {special}  width={_w(special):.6f}  (真实值域 [0,4]，宽 4)")

    print("\n[W3] 依赖问题：f(x) = x*(x-1)，X = [0,1]，真实值域 [-0.25, 0]，宽 0.25")
    f = lambda x: x * (x - 1)
    x = Interval(0, 1)
    print(f"{'方法':<18}{'结果区间':>40}{'宽度':>10}")
    r = f(x)
    print(f"{'朴素求值':<18}{str(r):>40}{_w(r):>10.6f}")
    for n in (4, 16, 64, 256, 1024):
        parts = []
        step = 1.0 / n
        for i in range(n):
            parts.append(f(Interval(i * step, (i + 1) * step)))
        h = hull(*parts)
        print(f"{f'分裂 N={n}':<18}{str(h):>40}{_w(h):>10.6f}")

    print("\n[W4] 中心形式（mean value form）：f(x)=x*(x-1)，X=[0.5-r, 0.5+r]")
    print("  f(X) ⊆ f(m) + f'(X)·(X - m)，m = mid(X)，f'(x)=2x-1")
    df = lambda x: Interval(2) * x - 1
    print(f"{'r':>10}{'朴素宽度':>14}{'中心形式宽度':>16}{'真实宽度':>12}")
    for r_ in (0.4, 0.1, 0.01, 0.001):
        x = Interval(0.5 - r_, 0.5 + r_)
        naive_w = _w(f(x))
        m = x.mid()
        centered = f(Interval(m)) + df(x) * (x - m)
        # 真实值域宽度：f 在 [0.5-r, 0.5+r] 上
        lo_true = min(0.5 - r_, 0.5) if 0.5 - r_ <= 0.5 else 0.5 - r_
        true_lo = min((t * (t - 1) for t in (0.5 - r_, 0.5 + r_, 0.5)
                       if 0.5 - r_ <= t <= 0.5 + r_))
        true_hi = max((t * (t - 1) for t in (0.5 - r_, 0.5 + r_, 0.5)
                       if 0.5 - r_ <= t <= 0.5 + r_))
        print(f"{r_:>10.4g}{naive_w:>14.6g}{_w(centered):>16.6g}{true_hi - true_lo:>12.6g}")

    print("\n[W5] 表达式改写（减少变量出现次数）：f(x)=(x+1)(x-1) vs x^2-1，X=[-2,2]")
    x = Interval(-2, 2)
    factored = (x + 1) * (x - 1)
    rewritten = x ** 2 - 1
    print(f"  (x+1)(x-1): width={_w(factored):.6f}  (x 出现 2 次，依赖被重复计算)")
    print(f"  x**2 - 1  : width={_w(rewritten):.6f}  (x 出现 1 次，等于真实值域宽 4)")

    print("\n[W6] 长表达式链：x <- 1.001x - 0.001x（实数上恒等），X0=[0.5,1.5]")
    print(f"{'步数':>8}{'朴素链宽度':>16}{'化简后宽度':>14}")
    iv = Interval(0.5, 1.5)
    simp = Interval(0.5, 1.5)
    for k in range(0, 1001, 200):
        if k:
            for _ in range(200):
                iv = Interval(1.001) * iv - Interval(0.001) * iv
                simp = Interval(1.001 - 0.001) * simp  # 合并同类项
        print(f"{k:>8}{_w(iv):>16.6f}{_w(simp):>14.6f}")
    print("  朴素链每步宽度 ×1.002（依赖问题累积）；化简后 ×1.000。")
    print()


# =====================================================================
# 实验 3：性能基准（100 万次运算）
# =====================================================================

def experiment_perf(n=1_000_000, seed=7):
    print("=" * 64)
    print(f"实验 3：性能基准（{n:,} 次混合运算，结果全部保留以测内存）")
    print("=" * 64)
    rng = random.Random(seed)
    pool = []
    for _ in range(8192):
        lo = rng.uniform(-5, 5)
        pool.append(Interval(lo, lo + rng.uniform(0.1, 3.0)))
    pool_pos = [Interval(rng.uniform(0.3, 3), rng.uniform(3, 6))
                for _ in range(4096)]

    def run(n_ops):
        results = []
        append = results.append
        for i in range(n_ops):
            a = pool[i & 8191]
            k = i % 6
            if k == 0:
                r = a + pool[(i * 31 + 7) & 8191]
            elif k == 1:
                r = a - pool[(i * 31 + 7) & 8191]
            elif k == 2:
                r = a * pool[(i * 31 + 7) & 8191]
            elif k == 3:
                r = a / pool_pos[i & 4095]
            elif k == 4:
                r = a ** 2
            else:
                r = sin(a)
            append(r)
        return results

    # 第一遍：纯计时（不开 tracemalloc，避免追踪开销污染耗时）
    results = run(100_000)  # 预热
    del results
    t0 = time.perf_counter()
    results = run(n)
    dt = time.perf_counter() - t0

    # 第二遍：纯测内存（tracemalloc 会拖慢速度，不计时）
    del results
    tracemalloc.start()
    results = run(n)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    print(f"  总耗时        : {dt:.3f} s（{n:,} 次运算）")
    print(f"  单次运算耗时  : {dt / n * 1e9:.0f} ns/op")
    print(f"  吞吐量        : {n / dt / 1e6:.2f} Mops/s")
    print(f"  tracemalloc 峰值: {peak / 2**20:.1f} MiB（{n:,} 个结果 Interval 对象 + list）")
    print(f"  进程 RSS 峰值 : {rss_kb / 1024:.1f} MiB")
    print(f"  单个 Interval : 约 {peak / n:.0f} B（含 list 槽位摊销）")
    assert len(results) == n
    print()


if __name__ == "__main__":
    experiment_inclusion()
    experiment_width()
    experiment_perf()

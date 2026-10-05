"""自测：包含性随机验证、特殊场景、宽度增长对比、百万次运算基准。

运行：python3 selftest.py
"""

import math
import random
import resource
import sys
import time
import tracemalloc

from interval_lib import (
    Affine,
    Interval,
    IntervalDivisionByZero,
    IntervalUnion,
    div_extended,
)

SEED = 20261005
N_RANDOM = 20000  # 每种运算的随机验证次数


def rand_interval(rng, lo=-10.0, hi=10.0, allow_zero=True):
    a, b = rng.uniform(lo, hi), rng.uniform(lo, hi)
    if a > b:
        a, b = b, a
    if not allow_zero and a <= 0.0 <= b:
        b = -abs(b) - 1e-3 if rng.random() < 0.5 else abs(a) + 1e-3
        if a > b:
            a, b = b, a
    return Interval(a, b)


def sample(rng, iv):
    if iv.lo == iv.hi:
        return iv.lo
    return rng.uniform(iv.lo, iv.hi)


def check_inclusion(name, n, gen):
    """gen(rng) -> (result_interval_or_union, true_value)；验证 true ∈ result。"""
    rng = random.Random(f"{SEED}:{name}")
    violations = 0
    first = None
    for _ in range(n):
        try:
            result, truth = gen(rng)
        except (IntervalDivisionByZero, OverflowError, ValueError, ZeroDivisionError):
            continue
        ok = truth in result
        if not ok:
            violations += 1
            if first is None:
                first = (result, truth)
    status = "PASS" if violations == 0 else "FAIL"
    print(f"  [{status}] {name:<28s} 验证 {n:>6d} 次, 违例 {violations}")
    if first:
        print(f"        首个违例: result={first[0]!r} truth={first[1]!r}")
    return violations == 0


def sec_inclusion():
    print("=" * 72)
    print(f"A. 包含性随机验证（每种运算 {N_RANDOM} 次, 种子 {SEED}）")
    print("   判定: 在输入区间内随机取真实点, 逐点计算真实值, 必须落在结果区间内")
    print("=" * 72)
    ok = True
    ok &= check_inclusion("x + y", N_RANDOM, lambda r: (
        (lambda a, b: (a + b, sample(r, a) + sample(r, b)))
        (rand_interval(r), rand_interval(r))))
    ok &= check_inclusion("x - y", N_RANDOM, lambda r: (
        (lambda a, b: (a - b, sample(r, a) - sample(r, b)))
        (rand_interval(r), rand_interval(r))))
    ok &= check_inclusion("x * y", N_RANDOM, lambda r: (
        (lambda a, b: (a * b, sample(r, a) * sample(r, b)))
        (rand_interval(r), rand_interval(r))))
    ok &= check_inclusion("x / y (y 不含 0)", N_RANDOM, lambda r: (
        (lambda a, b: (a / b, sample(r, a) / sample(r, b)))
        (rand_interval(r), rand_interval(r, allow_zero=False))))
    ok &= check_inclusion("x / y 扩展除法(y 含 0)", N_RANDOM, lambda r: (
        (lambda a, b: (div_extended(a, b), sample(r, a) / sample(r, b)))
        (rand_interval(r), rand_interval(r, -2, 2))))
    ok &= check_inclusion("x ** 2", N_RANDOM, lambda r: (
        (lambda a: (a ** 2, sample(r, a) ** 2))(rand_interval(r))))
    ok &= check_inclusion("x ** 3", N_RANDOM, lambda r: (
        (lambda a: (a ** 3, sample(r, a) ** 3))(rand_interval(r))))
    ok &= check_inclusion("x ** -2 (x 不含 0)", N_RANDOM, lambda r: (
        (lambda a: (a ** -2, sample(r, a) ** -2))(rand_interval(r, allow_zero=False))))
    ok &= check_inclusion("exp(x)", N_RANDOM, lambda r: (
        (lambda a: (a.exp(), math.exp(sample(r, a))))(rand_interval(r, -5, 5))))
    ok &= check_inclusion("log(x)", N_RANDOM, lambda r: (
        (lambda a: (a.log(), math.log(sample(r, a))))(rand_interval(r, 1e-6, 10))))
    ok &= check_inclusion("sqrt(x)", N_RANDOM, lambda r: (
        (lambda a: (a.sqrt(), math.sqrt(sample(r, a))))(rand_interval(r, 0, 10))))
    ok &= check_inclusion("sin(x)", N_RANDOM, lambda r: (
        (lambda a: (a.sin(), math.sin(sample(r, a))))(rand_interval(r, -20, 20))))
    ok &= check_inclusion("cos(x)", N_RANDOM, lambda r: (
        (lambda a: (a.cos(), math.cos(sample(r, a))))(rand_interval(r, -20, 20))))
    ok &= check_inclusion("abs(x)", N_RANDOM, lambda r: (
        (lambda a: (a.abs(), abs(sample(r, a))))(rand_interval(r))))

    # 函数复合：随机表达式链
    def chain(r):
        x = rand_interval(r, 0.5, 3.0)
        t = sample(r, x)
        res = x
        truth = t
        for _ in range(r.randint(3, 12)):
            op = r.choice(["add", "mul", "sq", "exp", "sin", "sqrt"])
            if op == "add":
                k = r.uniform(-2, 2); res = res + k; truth = truth + k
            elif op == "mul":
                k = r.uniform(0.5, 1.5); res = res * k; truth = truth * k
            elif op == "sq":
                res = res ** 2; truth = truth ** 2
            elif op == "exp":
                res = res.exp(); truth = math.exp(truth)
                if res.width > 1e6:
                    return res, truth  # 防溢出, 已足够长
            elif op == "sin":
                res = res.sin(); truth = math.sin(truth)
            else:
                if res.hi < 0.0:
                    res = res.abs(); truth = abs(truth)
                res = res.sqrt(); truth = math.sqrt(truth)
        return res, truth
    ok &= check_inclusion("复合链(3~12 层)", N_RANDOM, chain)

    # 开区间端点包含性
    def open_chain(r):
        a = rand_interval(r)
        b = rand_interval(r)
        ao = Interval(a.lo, a.hi, lo_open=r.random() < 0.5, hi_open=r.random() < 0.5)
        bo = Interval(b.lo, b.hi, lo_open=r.random() < 0.5, hi_open=r.random() < 0.5)
        res = (ao + bo) * ao - bo
        x = sample(r, ao); y = sample(r, bo)
        return res, (x + y) * x - y
    ok &= check_inclusion("开端点混合运算", N_RANDOM, open_chain)
    print()
    return ok


def sec_scenarios():
    print("=" * 72)
    print("B. 特殊场景覆盖")
    print("=" * 72)
    ok = True

    p = Interval.point(3.5)
    r1 = (p + p) * p - p
    expect = (3.5 + 3.5) * 3.5 - 3.5
    good = expect in r1 and r1.width < 1e-12
    ok &= good
    print(f"  [{'PASS' if good else 'FAIL'}] 单点区间: (([3.5]+[3.5])*[3.5])-[3.5] = {r1!r}")
    print(f"        外向舍入使单点运算结果宽 {r1.width:.2e} (~1 ulp), 真值 {expect} 仍在内")

    x = Interval(1.0, 2.0)
    d = x - x
    good = 0.0 in d and abs(d.width - 2.0) < 1e-12
    ok &= good
    print(f"  [{'PASS' if good else 'FAIL'}] 减法退化: x-x, x=[1,2] -> {d!r} (宽≈2, 含真实值 0; 区间算术无法识别同一变量)")

    try:
        Interval(1, 2) / Interval(-1, 1)
        good = False
        msg = "未抛异常!"
    except IntervalDivisionByZero as e:
        good = True
        msg = f"抛 IntervalDivisionByZero: {e}"
    ok &= good
    print(f"  [{'PASS' if good else 'FAIL'}] 除以含零区间(普通 /): {msg}")

    u = div_extended(Interval(1, 2), Interval(-1, 2))
    good = isinstance(u, IntervalUnion) and not u.is_bounded and (0.5 in u) and (-2.0 in u) and (0.0 not in u)
    ok &= good
    print(f"  [{'PASS' if good else 'FAIL'}] 除以含零区间(扩展): [1,2]/[-1,2] = {u!r}")
    print(f"        显式无界并集, 0 不在其中, 不是\"看似正常\"的区间")

    try:
        Interval(1, 2) / Interval(0, 0)
        good = False
    except IntervalDivisionByZero:
        good = True
    ok &= good
    print(f"  [{'PASS' if good else 'FAIL'}] 除以点区间 [0,0]: 抛 IntervalDivisionByZero")

    o = Interval(0.0, 1.0, lo_open=True, hi_open=True)
    good = (0.0 not in o) and (1.0 not in o) and (0.5 in o)
    o2 = o * Interval(2, 3)
    good &= o2.lo_open and o2.hi_open and (2.5 in o2)
    good &= (o2.hi not in o2) and (o2.lo not in o2)
    o3 = o * Interval(2, 3, hi_open=True)
    good &= o3.hi_open
    ok &= good
    print(f"  [{'PASS' if good else 'FAIL'}] 开区间端点: (0,1) 不含 0/1; (0,1)*[2,3] = {o2!r} (两端开)")
    print(f"        外向舍入把数值端点扩到相邻浮点数, 端点开闭靠 lo_open/hi_open 标志显式表达")

    rng = random.Random(SEED)
    acc = Interval.point(1.0)
    truth = 1.0
    for i in range(200):
        acc = acc * Interval(0.999, 1.001) + Interval(-1e-3, 1e-3)
        truth = truth * rng.uniform(0.999, 1.001) + rng.uniform(-1e-3, 1e-3)
    good = truth in acc
    ok &= good
    print(f"  [{'PASS' if good else 'FAIL'}] 长表达式链(200 步): 最终宽 {acc.width:.4f}, 真值 {truth:.4f} ∈ 结果: {good}")
    print()
    return ok


def sec_width():
    print("=" * 72)
    print("C. 区间宽度增长数据 与 缓解做法对比")
    print("=" * 72)

    print("  C1. 依赖问题: y = x - n·x (x = [1, 1.001], 真实宽 = (n-1)·0.001)")
    print(f"  {'n':>4s} {'朴素区间宽':>14s} {'仿射形式宽':>14s} {'真实宽':>12s}")
    for n in (1, 2, 4, 8, 16, 32):
        xi = Interval(1.0, 1.001)
        xa = Affine.from_interval(xi)
        yi = xi
        ya = xa
        for _ in range(n):
            yi = yi - xi
            ya = ya - xa
        true_w = abs(1 - n) * 0.001
        print(f"  {n:>4d} {yi.width:>14.6f} {ya.to_interval().width:>14.6f} {true_w:>12.6f}")
    print("  -> 缓解做法①: 仿射形式共享噪声符号, 相关误差抵消, 宽度 = 真实宽度 (朴素区间每步多加一个 x 的宽度)")

    print()
    print("  C2. 求值格式: p(x)=x^2-x, x=[0,1] (朴素幂次 vs Horner 嵌套 x(x-1))")
    xi = Interval(0.0, 1.0)
    naive = xi ** 2 - xi
    horner = xi * (xi - 1.0)
    print(f"      朴素 x^2-x: {naive!r} 宽 {naive.width:.6f}")
    print(f"      Horner x(x-1): {horner!r} 宽 {horner.width:.6f}")
    print(f"      真实值域: [-0.25, 0] 宽 0.25")
    print("  -> 缓解做法②: 因式分解/Horner 让同一变量只出现一次, 本例宽度 2.0 -> 1.0 (缩小 2 倍)")

    print()
    print("  C3. 相关减法长链: 初值 u=v=x=[1,1.01]; 每步 d=v-u; u=u+d/2; v=v-d/2")
    print("      真实: 一步后 u=v=(u+v)/2, 之后恒等于 x, 真实宽恒为 0.01")
    print(f"  {'步数':>4s} {'朴素区间 u 宽':>14s} {'仿射形式 u 宽':>14s} {'真实宽':>10s}")
    ui = vi = Interval(1.0, 1.01)
    ua = va = Affine.from_interval(Interval(1.0, 1.01))
    for k in range(1, 21):
        di = vi - ui
        ui, vi = ui + 0.5 * di, vi - 0.5 * di
        da = va - ua
        ua, va = ua + 0.5 * da, va - 0.5 * da
        if k in (1, 5, 10, 20):
            print(f"  {k:>4d} {ui.width:>14.6f} {ua.to_interval().width:>14.6f} {0.01:>10.6f}")
    print("  -> 缓解做法③: 朴素区间在每个 v-u 处退化为双倍宽并逐链累积; 仿射形式共享噪声")
    print("     符号, d=v-u 恒为 0, 链长 20 步仍保持真实宽度 0.01")
    print("     其它做法: 分裂区间(把输入细分后分别求值再取并)、中心形式(中点+半径)、")
    print("     泰勒模型(高阶项+余项区间), 均可进一步压缩宽度, 代价是计算量增加。")
    print()


def sec_benchmark():
    print("=" * 72)
    print("D. 100 万次运算基准 (CPython, 无第三方库)")
    print("=" * 72)
    rng = random.Random(SEED)
    n = 1_000_000

    t0 = time.perf_counter()
    a = Interval(0.9, 1.1)
    b = Interval(1.9, 2.1)
    acc = Interval.point(0.0)
    for i in range(n):
        acc = acc + a
        acc = acc - b
        acc = acc * Interval(0.5, 0.5)
        acc = acc + b
        if not (acc.lo <= acc.hi):
            raise RuntimeError("corrupt")
    t1 = time.perf_counter()
    ops = 4 * n
    print(f"  混合四则 {ops} 次: {t1 - t0:.3f} s ({ops / (t1 - t0) / 1e6:.2f} M ops/s)")

    tracemalloc.start()
    t0 = time.perf_counter()
    ivs = [Interval(rng.uniform(-1, 0), rng.uniform(0, 1)) for _ in range(n)]
    t1 = time.perf_counter()
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"  创建并持有 {n} 个区间: {t1 - t0:.3f} s, tracemalloc 峰值 {peak / 1e6:.1f} MB"
          f" (含列表; 单个 Interval 对象 {sys.getsizeof(ivs[0])} 字节, __slots__ 无 __dict__)")
    del ivs

    t0 = time.perf_counter()
    s = Interval(0.0, 1.0)
    for i in range(n):
        s = s.sin()
    t1 = time.perf_counter()
    print(f"  sin {n} 次: {t1 - t0:.3f} s ({n / (t1 - t0) / 1e6:.2f} M ops/s)")

    ru = resource.getrusage(resource.RUSAGE_SELF)
    print(f"  进程峰值 RSS (ru_maxrss): {ru.ru_maxrss / 1024:.1f} MB")
    print()


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, 种子 {SEED}")
    ok = sec_inclusion()
    ok &= sec_scenarios()
    sec_width()
    sec_benchmark()
    print("总体:", "全部通过" if ok else "存在失败项")
    sys.exit(0 if ok else 1)

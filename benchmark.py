"""100 万字符规模基准：检测耗时 + 内存峰值（tracemalloc）。

运行：python3 benchmark.py
"""
import random
import resource
import time
import tracemalloc

from periods import (
    min_period, exact_repetition, approximate_repeat, find_approximate_period,
)

N = 1_000_000


def build_cases():
    rng = random.Random(42)
    cases = {}
    cases["全同字符 a*1M"] = "a" * N
    cases["精确重复 abcd*250k"] = "abcd" * (N // 4)
    cases["无周期(随机,5字符)"] = "".join(rng.choice("abcde") for _ in range(N))
    noisy = list("abcd" * (N // 4))
    for _ in range(N // 100):  # 1% 随机突变
        i = rng.randrange(N)
        noisy[i] = rng.choice("xyz")
    cases["近似重复(1%差异)"] = "".join(noisy)
    return cases


def measure(fn):
    t0 = time.perf_counter()
    result = fn()
    dt = time.perf_counter() - t0
    tracemalloc.start()
    fn()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, dt, peak / 1024 / 1024


def main():
    cases = build_cases()
    print(f"串长 n = {N:,}\n")
    print(f"{'情形':<22}{'检测':<26}{'结果':<44}{'耗时(s)':>9}{'峰值(MB)':>10}")
    for name, s in cases.items():
        res, dt, mem = measure(lambda: min_period(s))
        print(f"{name:<24}{'min_period':<26}{res:<44}{dt:>9.3f}{mem:>10.1f}")
        res, dt, mem = measure(lambda: exact_repetition(s))
        print(f"{'':<24}{'exact_repetition':<26}{str(res):<44}{dt:>9.3f}{mem:>10.1f}")
        if name.startswith("近似重复"):
            (p, est), dt, mem = measure(
                lambda: find_approximate_period(s, p_max=64, samples=4000, seed=7))
            print(f"{'':<24}{'find_approx(p<=64,抽样)':<26}"
                  f"{f'p={p}, 估计失配率={est:.4f}':<44}{dt:>9.3f}{mem:>10.1f}")
            r, dt, mem = measure(lambda: approximate_repeat(s, p, max_rate=0.05))
            desc = (f"失配 {r['mismatches']:,}/{r['compared']:,} "
                    f"率={r['rate']:.4f} 容差={r['allowed']:,} "
                    f"通过={r['within_tolerance']} 前5个位置={r['positions'][:5]}")
            print(f"{'':<24}{'approximate_repeat(p)':<26}{desc:<44}{dt:>9.3f}{mem:>10.1f}")
    print(f"\n进程峰值 RSS: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.1f} MB")


if __name__ == "__main__":
    main()

"""sliding_median 的自测与实测脚本。

运行：python3 selftest.py

包含：
1. 与朴素实现（每窗口重新排序）逐窗口对拍，并打印对拍数据样例；
2. 边界用例：k=1、k=n、全部相同、窗口内含极端值；
3. 大量重复值 / 全同数据下删除逻辑的正确性；
4. 复杂度实测：耗时随 n 大致线性增长；
5. 100 万元素、窗口 1000 的耗时与内存峰值。
"""

import random
import resource
import sys
import time
import tracemalloc

from sliding_median import SlidingMedian, sliding_median


# ---------------------------------------------------------------- 朴素实现

def naive_median(window):
    s = sorted(window)
    m = len(s)
    if m % 2 == 1:
        return float(s[m // 2])
    return (s[m // 2 - 1] + s[m // 2]) / 2.0


def naive_sliding_median(nums, k):
    return [naive_median(nums[i:i + k]) for i in range(len(nums) - k + 1)]


# ---------------------------------------------------------------- 工具

def assert_close(got, want, label):
    assert len(got) == len(want), f"{label}: 长度不一致 {len(got)} != {len(want)}"
    for i, (a, b) in enumerate(zip(got, want)):
        assert a == b, f"{label}: 第 {i} 个窗口不一致 fast={a} naive={b}"
    print(f"[PASS] {label}（{len(got)} 个窗口全部一致）")


def show_pairs(nums, k, got, want, count=8):
    """打印前 count 个窗口的对拍数据。"""
    print(f"    {'窗口下标':>6}  {'窗口内容':<44} {'朴素中位数':>12} {'双堆中位数':>12}")
    for i in range(min(count, len(got))):
        w = nums[i:i + k]
        wtxt = str(w)
        if len(wtxt) > 42:
            wtxt = wtxt[:39] + "..."
        print(f"    {i:>8}  {wtxt:<44} {want[i]:>12} {got[i]:>12}")


# ---------------------------------------------------------------- 1. 对拍

def test_against_naive():
    print("== 1. 与朴素实现逐窗口对拍 ==")
    rng = random.Random(20261005)

    cases = [
        ("随机整数 n=2000 k=7",   [rng.randint(-100, 100) for _ in range(2000)], 7),
        ("随机整数 n=3000 k=100（偶数窗）", [rng.randint(0, 10**6) for _ in range(3000)], 100),
        ("随机浮点 n=1500 k=33",  [rng.uniform(-1e6, 1e6) for _ in range(1500)], 33),
        ("大量重复值 n=5000 k=50（取值仅 3 种）", [rng.choice([1, 2, 3]) for _ in range(5000)], 50),
        ("全同数据 n=4000 k=41",  [7] * 4000, 41),
        ("含极端值 n=2000 k=25",  [rng.choice([-(10**18), 10**18, 0, rng.randint(-5, 5)]) for _ in range(2000)], 25),
    ]

    demo_shown = False
    for label, nums, k in cases:
        got = sliding_median(nums, k)
        want = naive_sliding_median(nums, k)
        assert_close(got, want, label)
        if not demo_shown:
            print("  对拍数据样例（取自用例「%s」的前 8 个窗口）：" % label)
            show_pairs(nums, k, got, want)
            demo_shown = True


# ---------------------------------------------------------------- 2. 边界

def test_edge_cases():
    print("\n== 2. 边界用例 ==")

    nums = [5, 1, 9, 3, 7]
    got = sliding_median(nums, 1)
    want = naive_sliding_median(nums, 1)
    assert_close(got, want, "窗口为 1（k=1，中位数即元素本身）")
    show_pairs(nums, 1, got, want, count=5)

    nums = [4, 2, 8, 6, 10, 1]
    got = sliding_median(nums, len(nums))
    want = naive_sliding_median(nums, len(nums))
    assert_close(got, want, "窗口等于序列长度（k=n，只有 1 个窗口）")
    show_pairs(nums, len(nums), got, want, count=1)

    nums = [3] * 10
    got = sliding_median(nums, 4)
    want = naive_sliding_median(nums, 4)
    assert_close(got, want, "全部相同（k=4，偶数窗取平均仍是 3.0）")
    show_pairs(nums, 4, got, want, count=3)

    nums = [-(10**18), 10**18, 0, 10**18, -(10**18), 5, -5]
    got = sliding_median(nums, 3)
    want = naive_sliding_median(nums, 3)
    assert_close(got, want, "窗口内有极端值（±1e18 交替）")
    show_pairs(nums, 3, got, want, count=5)


# ---------------------------------------------------------------- 3. 重复值/全同

def test_duplicates():
    print("\n== 3. 大量重复值与全同数据 ==")
    rng = random.Random(7)

    nums = [rng.choice([0, 0, 0, 1]) for _ in range(20000)]
    got = sliding_median(nums, 999)
    want = naive_sliding_median(nums, 999)
    assert_close(got, want, "75% 重复值 n=20000 k=999")

    nums = [42] * 100000
    got = sliding_median(nums, 1000)
    want = naive_sliding_median(nums, 1000)
    assert_close(got, want, "全同数据 n=100000 k=1000")
    print(f"    全同数据结果：共 {len(got)} 个窗口，"
          f"首={got[0]} 中={got[len(got)//2]} 尾={got[-1]}（全部应为 42.0）")
    assert all(v == 42.0 for v in got)


# ---------------------------------------------------------------- 4. 线性度实测

def bench_linearity():
    print("\n== 4. 复杂度实测：耗时随 n 大致线性（k=1000 固定） ==")
    rng = random.Random(1)
    print(f"    {'n':>10} {'k':>6} {'耗时(s)':>10} {'每元素耗时(us)':>14} {'相对上一档倍率':>14}")
    prev_t = None
    for n in (125_000, 250_000, 500_000, 1_000_000):
        nums = [rng.randint(-10**9, 10**9) for _ in range(n)]
        t0 = time.perf_counter()
        res = sliding_median(nums, 1000)
        dt = time.perf_counter() - t0
        ratio = f"{dt / prev_t:>13.2f}x" if prev_t else f"{'-':>14}"
        print(f"    {n:>10} {1000:>6} {dt:>10.3f} {dt / n * 1e6:>14.2f} {ratio}")
        prev_t = dt
        del nums, res
    print("    n 每翻倍，耗时倍率应接近 2x（O(n log k)，log k 为常数）。")


# ---------------------------------------------------------------- 5. 百万级 benchmark

def bench_million():
    print("\n== 5. 100 万元素、窗口 1000：耗时与内存峰值 ==")
    rng = random.Random(2)
    n, k = 1_000_000, 1000
    nums = [rng.randint(-10**9, 10**9) for _ in range(n)]

    # 计时：不挂 tracemalloc，避免插桩拖慢
    t0 = time.perf_counter()
    res = sliding_median(nums, k)
    dt = time.perf_counter() - t0

    # 内存：单独再跑一遍，手动驱动维护器、不收集结果列表，
    # 用 tracemalloc 统计维护结构（两个堆 + 延迟删除表）的内存峰值
    tracemalloc.start()
    sm = SlidingMedian(k)
    last = None
    for i, x in enumerate(nums):
        sm.add(x)
        if i >= k:
            sm.remove(nums[i - k])
        if i >= k - 1:
            last = sm.median()
    _, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # Linux: KiB
    print(f"    窗口数        : {len(res)}")
    print(f"    总耗时        : {dt:.3f} s（每元素 {dt / n * 1e6:.2f} us）")
    print(f"    维护结构内存峰值: {py_peak / 1024:.1f} KiB（tracemalloc，双堆+延迟表，不含结果列表）")
    print(f"    进程 RSS 峰值 : {rss_kb / 1024:.2f} MiB（ru_maxrss，含输入数组与解释器）")

    # 抽样校验前 3 个窗口
    want = naive_sliding_median(nums[: k + 2], k)
    assert res[:3] == want, "百万级结果抽样校验失败"
    print(f"    抽样校验      : 前 3 个窗口与朴素实现一致 -> {res[:3]}")


def main():
    print(f"Python {sys.version.split()[0]}\n")
    test_against_naive()
    test_edge_cases()
    test_duplicates()
    bench_linearity()
    bench_million()
    print("\n全部测试通过。")


if __name__ == "__main__":
    main()

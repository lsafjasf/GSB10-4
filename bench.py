"""基准：100 万行 x 100 万行，报告各关联的耗时与内存峰值。

内存峰值来源：resource.getrusage(RUSAGE_SELF).ru_maxrss（进程级峰值 RSS，
Linux 单位 KiB），并对 inner_join 用 tracemalloc 记录 Python 堆分配峰值。
"""
import resource
import time
import tracemalloc

import null_join as nj

N = 1_000_000
NULL_RATIO_L = 0.10
NULL_RATIO_R = 0.10
KEY_DOMAIN = 800_000  # 键域 < 行数 => 存在重复键(多对多)


def gen(n, null_ratio, tag):
    # 确定性伪随机，避免引入 random 模块开销进计时；数据本身可复现
    rows = []
    seed = 123456789
    for i in range(n):
        seed = (seed * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        k = seed % 1_000_000
        key = None if k < null_ratio * 1_000_000 else k % KEY_DOMAIN
        rows.append((key, f"{tag}{i}"))
    return rows


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main():
    print(f"数据规模: 左 {N:,} 行 x 右 {N:,} 行, 左空外键 {NULL_RATIO_L:.0%}, "
          f"右空键 {NULL_RATIO_R:.0%}, 键域 {KEY_DOMAIN:,}")
    t0 = time.perf_counter()
    left = gen(N, NULL_RATIO_L, "L")
    right = gen(N, NULL_RATIO_R, "R")
    t1 = time.perf_counter()
    print(f"数据生成: {t1 - t0:.2f}s, 峰值RSS {rss_mb():.0f} MiB")
    print(f"{'join':<8} {'耗时(s)':>8} {'结果行数':>12} {'峰值RSS(MiB)':>14}")

    jobs = [
        ("inner", lambda: nj.inner_join(left, right, 0, 0)),
        ("left",  lambda: nj.left_join(left, right, 0, 0)),
        ("right", lambda: nj.right_join(left, right, 0, 0)),
        ("full",  lambda: nj.full_join(left, right, 0, 0)),
        ("semi",  lambda: nj.semi_join(left, right, 0, 0)),
        ("anti",  lambda: nj.anti_join(left, right, 0, 0)),
    ]
    for name, fn in jobs:
        if name == "inner":
            tracemalloc.start()
        start = time.perf_counter()
        out = fn()
        elapsed = time.perf_counter() - start
        if name == "inner":
            _, py_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            print(f"  [inner_join Python 堆分配峰值(tracemalloc): {py_peak/2**20:.0f} MiB]")
        print(f"{name:<8} {elapsed:>8.2f} {len(out):>12,} {rss_mb():>14.0f}")
        del out


if __name__ == "__main__":
    main()

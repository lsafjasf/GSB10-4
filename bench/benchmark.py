"""性能压测：100 万行 关联 100 万行。

数据：左表 100 万行 (id, payload)，id 唯一；
      右表 100 万行 (fk, payload)，90% 随机指向左表 id，10% 为空外键。
指标：关联耗时（tracemalloc 计时的 join 调用本身）、
      join 期间 Python 堆内存峰值（tracemalloc）、
      全进程常驻内存峰值（resource.ru_maxrss）。
运行：python3 bench/benchmark.py
"""

import os
import random
import resource
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nulljoin import JoinType, join

N = 1_000_000


def gen_data(n, null_ratio=0.1, seed=42):
    rng = random.Random(seed)
    left = [(i, "L%d" % i) for i in range(1, n + 1)]
    right = []
    for i in range(n):
        fk = None if rng.random() < null_ratio else rng.randrange(1, n + 1)
        right.append((fk, "R%d" % i))
    return left, right


def bench(how, left, right):
    tracemalloc.start()
    t0 = time.perf_counter()
    out = join(left, right, 0, 0, how)
    dt = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return out, dt, peak


def main():
    print("生成数据：左 %d 行，右 %d 行（10%% 空外键）..." % (N, N))
    t0 = time.perf_counter()
    left, right = gen_data(N)
    print("数据生成耗时 %.2f s" % (time.perf_counter() - t0))

    results = []
    for how in (JoinType.INNER, JoinType.LEFT, JoinType.LEFT_SEMI, JoinType.LEFT_ANTI):
        out, dt, peak = bench(how, left, right)
        results.append((how.value, len(out), dt, peak))
        print("%-10s 结果 %9d 行  耗时 %6.2f s  join 堆峰值 %7.1f MiB"
              % (how.value, len(out), dt, peak / 2**20))
        del out

    # 独立校验：内连接行数应等于右表非空外键且命中左表的行数
    left_keys = {r[0] for r in left}
    expect = sum(1 for r in right if r[0] is not None and r[0] in left_keys)
    got = results[0][1]
    assert got == expect, (got, expect)
    print("独立校验：inner 结果行数 %d == 期望 %d  ok" % (got, expect))

    rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    print("全进程常驻内存峰值 ru_maxrss = %.1f MiB" % rss_mib)

    report = os.path.join(os.path.dirname(__file__), "..", "reports", "benchmark.md")
    with open(report, "w", encoding="utf-8") as f:
        f.write("# 100 万 x 100 万 关联压测\n\n")
        f.write("- 数据：左表 100 万行 `(id, payload)`，id 唯一；右表 100 万行 `(fk, payload)`，"
                "90% 随机指向左表，10% 为空外键（seed=42）\n")
        f.write("- 环境：Python %s，%d 核，tracemalloc 计量 join 期间堆峰值，"
                "ru_maxrss 计量全进程峰值\n\n" % (sys.version.split()[0], os.cpu_count()))
        f.write("| 关联类型 | 结果行数 | 耗时 (s) | join 堆峰值 (MiB) |\n")
        f.write("|---|---:|---:|---:|\n")
        for name, rows, dt, peak in results:
            f.write("| %s | %d | %.2f | %.1f |\n" % (name, rows, dt, peak / 2**20))
        f.write("\n全进程常驻内存峰值（含输入数据与结果）：%.1f MiB\n" % rss_mib)
    print("报告已写入", os.path.relpath(report))


if __name__ == "__main__":
    main()

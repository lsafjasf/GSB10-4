"""100 万样本校准基准：python3 benchmark.py [N]

流式生成样本（generator），校准过程不需要把全部样本驻留内存。
"""
import random
import resource
import sys
import time
import tracemalloc

from costcalib import calibrate

TRUE_W = (12.0, 0.02, 1.5, -0.7, 0.001, 0.0008)   # 截距 + 5 个特征
BAD_W = (50.0, 0.05, 0.5, 0.3, 0.01, 0.003)       # 拍出来的初始系数


def gen(n, seed=20261005, noise=2.0):
    rng = random.Random(seed)
    for _ in range(n):
        x = [rng.uniform(0, 1000), rng.uniform(0, 100),
             rng.uniform(0, 500), rng.uniform(0, 20000),
             rng.uniform(0, 10000)]
        y = TRUE_W[0] + sum(w * v for w, v in zip(TRUE_W[1:], x)) + rng.gauss(0, noise)
        yield x, y


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    print(f"样本数: {n:,}  特征数: {len(TRUE_W) - 1}")

    tracemalloc.start()
    t0 = time.perf_counter()
    r = calibrate(gen(n), BAD_W, seed=42)
    elapsed = time.perf_counter() - t0
    cur, peak_py = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    print(f"状态: {r.status} ({r.reason})  训练 {r.n_train:,} / 留出 {r.n_holdout:,}  PSI={r.psi_max:.4f}")
    print(f"耗时: {elapsed:.2f} s  ({elapsed / n * 1e6:.2f} us/样本)")
    print(f"Python 对象内存峰值(tracemalloc): {peak_py / 1024 / 1024:.1f} MiB")
    print(f"进程 RSS 峰值(getrusage): {rss_kb / 1024:.1f} MiB")
    print(f"原系数:   {tuple(round(v, 6) for v in BAD_W)}")
    print(f"校准系数: {tuple(round(v, 6) for v in r.coefficients)}")
    print(f"真实系数: {TRUE_W}")
    print(f"{'':>8} {'MAE':>12} {'RMSE':>12} {'SMAPE':>10} {'R2':>10}")
    b, a = r.before, r.after
    print(f"{'校准前':>8} {b.mae:>12.4f} {b.rmse:>12.4f} {b.smape:>10.4f} {b.r2:>10.5f}")
    print(f"{'校准后':>8} {a.mae:>12.4f} {a.rmse:>12.4f} {a.smape:>10.4f} {a.r2:>10.5f}")
    print(f"RMSE 下降: {(1 - a.rmse / b.rmse) * 100:.2f}%")


if __name__ == "__main__":
    main()

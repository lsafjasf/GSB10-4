"""100 万条样本的校准性能基准：耗时 + 内存峰值 + 校准前后误差对比。

运行：python3 benchmark.py [n_samples]   （默认 1,000,000）
"""

import sys
import time
import tracemalloc
from random import Random

from cost_calibrator import calibrate

TRUE_COEF = [5.0, 1.0, 8.0, 0.02, 0.05]
BASELINE = [10.0, 2.0, 4.0, 0.05, 0.01]


def generate(n, seed=2024):
    """流式生成合成样本（不落地，校准端内存与 n 无关）。"""
    rng = Random(seed)
    for _ in range(n):
        x = (rng.lognormvariate(3.0, 1.0),
             rng.lognormvariate(1.0, 1.2),
             rng.lognormvariate(6.0, 1.0),
             rng.lognormvariate(3.0, 1.0))
        noise = rng.lognormvariate(0.0, 0.3)
        y = (TRUE_COEF[0] + sum(c * v for c, v in zip(TRUE_COEF[1:], x))) * noise
        yield x, y


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000

    # 数据生成本身的耗时（用于从总耗时中扣除，得到校准净耗时）
    t0 = time.perf_counter()
    for _ in generate(n):
        pass
    t_gen = time.perf_counter() - t0

    # 计时（不带内存插桩，避免测量开销污染耗时）
    t0 = time.perf_counter()
    report = calibrate(generate(n), BASELINE, seed=42)
    t_total = time.perf_counter() - t0

    # 内存峰值单独测量（tracemalloc 会拖慢运行，不计入耗时）
    tracemalloc.start()
    calibrate(generate(n), BASELINE, seed=42)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    b, c = report.baseline_metrics, report.calibrated_metrics
    print(f"samples           : {report.n_samples:,} "
          f"(train {report.n_train:,} / holdout {report.n_holdout:,})")
    print(f"total time        : {t_total:.2f} s")
    print(f"generation time   : {t_gen:.2f} s")
    print(f"calibration time  : {t_total - t_gen:.2f} s  (net, excl. data generation)")
    print(f"peak memory       : {peak / 1024 / 1024:.1f} MiB  (tracemalloc)")
    print(f"coefficients      : {[round(v, 6) for v in report.coefficients]}")
    print()
    print(f"{'metric':<14}{'before':>14}{'after':>14}{'ratio':>10}")
    for name in ("mae", "rmse", "mean_qerror", "median_qerror", "p95_qerror"):
        vb, vc = getattr(b, name), getattr(c, name)
        print(f"{name:<14}{vb:>14.4f}{vc:>14.4f}{vc / vb:>10.3f}")


if __name__ == "__main__":
    main()

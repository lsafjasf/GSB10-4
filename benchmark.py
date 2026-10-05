#!/usr/bin/env python3
"""sparse_lu 基准：重排填充对比、10 万阶/百万非零耗时与内存、内存超限拒绝演示。

用法:
  python3 benchmark.py            # 全部
  python3 benchmark.py --compare  # 仅重排填充对比表
  python3 benchmark.py --worker <name>   # 内部：子进程基准 worker
"""

import json
import math
import os
import random
import subprocess
import sys
import tempfile
import time

from sparse_lu import (
    banded_matrix,
    diagonal_matrix,
    near_dense_matrix,
    poisson_2d_matrix,
    random_sparse_matrix,
    min_degree_order,
    natural_order,
    rcm_order,
    symbolic_lu,
    factor,
    factor_residual,
    iterative_refinement,
    MemoryBudgetExceeded,
)
from sparse_lu.matrix import ill_conditioned_banded
from sparse_lu.memory import current_rss_bytes, max_rss_bytes

N_BIG = 100_000
BW_BIG = 5


def mib(x):
    return x / (1024 * 1024)


def scrambled(matrix, seed):
    perm = list(range(matrix.n))
    random.Random(seed).shuffle(perm)
    return matrix.permute_symmetric(perm)


# ---------------------------------------------------------------- 重排对比表

def compare_orderings():
    cases = [
        ("对角 n=20000", diagonal_matrix(20000)),
        ("带状 n=5000 bw=20", banded_matrix(5000, 20, seed=3)),
        ("Poisson 40x40 (九点)", poisson_2d_matrix(40, 40)),
        ("打乱 Poisson 40x40", scrambled(poisson_2d_matrix(40, 40), seed=5)),
        ("随机稀疏 n=2000 nnz~10000", random_sparse_matrix(2000, 10000, seed=4)),
        ("近稠密 n=200 d=0.9", near_dense_matrix(200, 0.9, seed=6)),
    ]
    orderings = [("natural", lambda a: natural_order(a.n)),
                 ("rcm", rcm_order),
                 ("mindeg", min_degree_order)]
    print("### 重排策略填充对比（符号分解，fill = nnz(L)+nnz(U) - nnz(A)）")
    header = f"{'矩阵':<26}{'nnz(A)':>10}" + "".join(
        f"{name+' LU nnz':>14}{name+' fill':>13}" for name, _ in orderings)
    print(header)
    print("-" * len(header))
    for cname, a in cases:
        row = f"{cname:<26}{a.nnz:>10}"
        for oname, ofn in orderings:
            t0 = time.perf_counter()
            perm = ofn(a)
            s = symbolic_lu(a.permute_symmetric(perm))
            dt = time.perf_counter() - t0
            row += f"{s.lu_nnz:>14}{s.fill:>13}"
            print(f"    [{oname:<8} {dt:6.2f}s]", end="", flush=True)
        print("\r" + row + " " * 22)


# ---------------------------------------------------------------- 大矩阵 worker

def _big_matrix(slack=1.0):
    return banded_matrix(N_BIG, BW_BIG, seed=1, slack=slack)


def _report(**kw):
    kw["peak_rss_mib"] = round(mib(max_rss_bytes() or 0), 1)
    print("JSON:" + json.dumps(kw))


def worker_big_natural():
    t0 = time.perf_counter()
    a = _big_matrix()
    t_build = time.perf_counter() - t0
    t0 = time.perf_counter()
    lu = factor(a, block_size=64)
    t_factor = time.perf_counter() - t0
    t0 = time.perf_counter()
    res = factor_residual(lu, a)
    t_res = time.perf_counter() - t0
    b = [1.0 + 1e-4 * i for i in range(a.n)]
    t0 = time.perf_counter()
    x, hist = iterative_refinement(a, lu, b, max_iter=5, tol=1e-13)
    t_refine = time.perf_counter() - t0
    _report(case="big-natural", n=a.n, nnz=a.nnz, lu_nnz=lu.stats["lu_nnz"],
            fill=lu.stats["fill"], t_build=round(t_build, 3),
            t_factor=round(t_factor, 3), t_residual=round(t_res, 3),
            t_solve_refine=round(t_refine, 3), factor_residual=f"{res:.3e}",
            refine_history=[(it, f"{r:.2e}") for it, r in hist])


def worker_big_rcm():
    a = _big_matrix()
    t0 = time.perf_counter()
    perm = rcm_order(a)
    t_order = time.perf_counter() - t0
    ap = a.permute_symmetric(perm)
    t0 = time.perf_counter()
    lu = factor(ap, block_size=64)
    t_factor = time.perf_counter() - t0
    res = factor_residual(lu, ap)
    _report(case="big-rcm", n=a.n, nnz=a.nnz, lu_nnz=lu.stats["lu_nnz"],
            fill=lu.stats["fill"], t_order=round(t_order, 3),
            t_factor=round(t_factor, 3), factor_residual=f"{res:.3e}")


def worker_big_spill():
    a = _big_matrix()
    tmp = tempfile.mkdtemp(prefix="sparse_lu_bench_spill_")
    t0 = time.perf_counter()
    lu = factor(a, block_size=64, spill_dir=tmp)
    t_factor = time.perf_counter() - t0
    spill_bytes = sum(os.path.getsize(os.path.join(tmp, f))
                      for f in os.listdir(tmp))
    b = [1.0 + 1e-4 * i for i in range(a.n)]
    t0 = time.perf_counter()
    x = lu.solve(b)
    t_solve = time.perf_counter() - t0
    from sparse_lu.refine import relative_residual
    res = relative_residual(a, x, b)
    _report(case="big-spill", n=a.n, nnz=a.nnz, lu_nnz=lu.stats["lu_nnz"],
            fill=lu.stats["fill"], t_factor=round(t_factor, 3),
            t_solve=round(t_solve, 3), panels=lu.stats["panels"],
            spill_mib=round(mib(spill_bytes), 1), solve_residual=f"{res:.2e}")


def worker_big_scrambled_rcm():
    a = scrambled(_big_matrix(), seed=7)
    t0 = time.perf_counter()
    perm = rcm_order(a)
    t_order = time.perf_counter() - t0
    ap = a.permute_symmetric(perm)
    t0 = time.perf_counter()
    lu = factor(ap, block_size=64)
    t_factor = time.perf_counter() - t0
    res = factor_residual(lu, ap)
    _report(case="big-scrambled-rcm", n=a.n, nnz=a.nnz,
            lu_nnz=lu.stats["lu_nnz"], fill=lu.stats["fill"],
            t_order=round(t_order, 3), t_factor=round(t_factor, 3),
            factor_residual=f"{res:.3e}")


def worker_big_refine():
    # 近奇异 M-矩阵（行和=1e-7，条件数 ~1e8）：初始残差即达 kappa*eps 下限
    a = ill_conditioned_banded(N_BIG, BW_BIG, slack=1e-7)
    lu = factor(a, block_size=64)
    b = [1.0 + 1e-4 * i for i in range(a.n)]
    x, hist = iterative_refinement(a, lu, b, max_iter=4, tol=1e-13)
    _report(case="big-refine", n=a.n, nnz=a.nnz,
            refine_history=[(it, f"{r:.2e}") for it, r in hist])


def worker_refuse_random():
    a = random_sparse_matrix(N_BIG, 1_000_000, seed=2)
    base = current_rss_bytes() or 0
    limit = base + 512 * 1024 * 1024
    t0 = time.perf_counter()
    try:
        factor(a, block_size=64, memory_limit_bytes=limit)
        _report(case="refuse-random", refused=False)
    except MemoryBudgetExceeded as exc:
        _report(case="refuse-random", refused=True,
                t_refuse=round(time.perf_counter() - t0, 3),
                live_entries=exc.live_entries,
                limit_mib=round(mib(exc.limit_bytes), 1),
                message=str(exc)[:160])


def worker_blocksize():
    a = _big_matrix()
    rows = []
    for bs in (1, 16, 64, 256, 1024):
        t0 = time.perf_counter()
        lu = factor(a, block_size=bs)
        rows.append((bs, round(time.perf_counter() - t0, 3),
                     lu.stats["panels"]))
    _report(case="blocksize", n=a.n, nnz=a.nnz, sweep=rows)


WORKERS = {
    "big-natural": worker_big_natural,
    "big-rcm": worker_big_rcm,
    "big-spill": worker_big_spill,
    "big-scrambled-rcm": worker_big_scrambled_rcm,
    "big-refine": worker_big_refine,
    "refuse-random": worker_refuse_random,
    "blocksize": worker_blocksize,
}


def run_worker(name):
    out = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--worker", name],
        capture_output=True, text=True, timeout=1800)
    for line in out.stdout.splitlines():
        if line.startswith("JSON:"):
            return json.loads(line[5:])
    raise RuntimeError(f"worker {name} 失败:\n{out.stdout}\n{out.stderr}")


def big_benchmark():
    print(f"### 大规模基准: n={N_BIG}, 半带宽={BW_BIG}, nnz(A)≈1.1M "
          f"(每个用例独立子进程，峰值内存为进程 ru_maxrss)")
    results = {}
    for name in ("big-natural", "big-rcm", "big-scrambled-rcm",
                 "big-spill", "big-refine", "blocksize", "refuse-random"):
        t0 = time.perf_counter()
        results[name] = run_worker(name)
        results[name]["wall"] = round(time.perf_counter() - t0, 2)
        print(f"    [{name} 完成 {results[name]['wall']}s]", flush=True)

    print("\n-- 分解耗时 / 内存峰值 / 填充 --")
    print(f"{'用例':<22}{'LU nnz':>12}{'fill':>10}{'分解s':>9}{'峰值MiB':>10}")
    for name in ("big-natural", "big-rcm", "big-scrambled-rcm", "big-spill"):
        r = results[name]
        print(f"{name:<22}{r['lu_nnz']:>12}{r['fill']:>10}"
              f"{r['t_factor']:>9}{r['peak_rss_mib']:>10}")
    r = results["big-natural"]
    print(f"\n-- 残差（自然序, n={r['n']}）--")
    print(f"||A-LU||_F/||A||_F = {r['factor_residual']}  "
          f"(耗时 {r['t_residual']}s)")
    print("迭代精化（强对角占优）: " +
          " ".join(f"it{it}:{v}" for it, v in r["refine_history"]))
    r = results["big-refine"]
    print("迭代精化（近奇异 M-矩阵, kappa~1e8）: " +
          " ".join(f"it{it}:{v}" for it, v in r["refine_history"]))

    r = results["big-spill"]
    print(f"\n-- 溢写 --  面板数={r['panels']} 溢写总量={r['spill_mib']} MiB "
          f"求解残差={r['solve_residual']} 分解={r['t_factor']}s "
          f"峰值={r['peak_rss_mib']} MiB")

    print("\n-- 块大小扫描（分块对耗时的影响）--")
    for bs, t, panels in results["blocksize"]["sweep"]:
        print(f"    B={bs:<6} 面板数={panels:<8} 分解耗时={t}s")

    r = results["refuse-random"]
    print("\n-- 内存超限拒绝演示（随机稀疏 100k, nnz=1.1M, 预算=基线+512MiB）--")
    if r["refused"]:
        print(f"    {r['t_refuse']}s 后安全拒绝: {r['message']}")
    else:
        print("    未触发拒绝（异常！）")


def main():
    args = sys.argv[1:]
    if args and args[0] == "--worker":
        WORKERS[args[1]]()
        return
    if "--compare" in args or not args:
        compare_orderings()
    if "--big" in args or not args:
        big_benchmark()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""sparse_lu 自测（仅标准库）：正确性、填充规则、重排、内存守卫、溢写、迭代精化。

运行: python3 selftest.py
"""

import math
import os
import tempfile
import time

from sparse_lu import (
    SparseMatrix,
    banded_matrix,
    diagonal_matrix,
    near_dense_matrix,
    poisson_2d_matrix,
    random_sparse_matrix,
    natural_order,
    rcm_order,
    min_degree_order,
    symbolic_lu,
    factor,
    factor_residual,
    iterative_refinement,
    MemoryBudgetExceeded,
    ZeroPivotError,
    MemoryGuard,
)
from sparse_lu.dense_ref import dense_lu, dense_residual
from sparse_lu.refine import relative_residual
from sparse_lu.memory import current_rss_bytes

FAILURES = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" -- {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def test_vs_dense(name, matrix, order_fn, block_size, tol=1e-9):
    perm = order_fn(matrix)
    ap = matrix.permute_symmetric(perm)
    lu = factor(ap, block_size=block_size)
    res = factor_residual(lu, ap)
    l0, u0 = dense_lu(ap)
    dres = dense_residual(ap, l0, u0)
    n = ap.n
    l_rows = {}
    u_rows = {}
    for p in lu.panels:
        for i, pl in p["L"].items():
            l_rows.setdefault(i, {}).update(pl)
        for i, ui in p["U"].items():
            u_rows[i] = ui
    max_err = 0.0
    for i in range(n):
        lr = l_rows.get(i, {})
        for j in range(i):
            max_err = max(max_err, abs(lr.get(j, 0.0) - l0[i][j]))
        ur = u_rows[i]
        for j in range(i, n):
            max_err = max(max_err, abs(ur.get(j, 0.0) - u0[i][j]))
    scale = ap.max_abs() or 1.0
    ok = res < tol and max_err / scale < 1e-9 and math.isfinite(res)
    check(f"稠密参考一致性: {name} B={block_size}", ok,
          f"残差={res:.3e} 稠密残差={dres:.3e} 逐元误差={max_err/scale:.3e} "
          f"A nnz={matrix.nnz} LU nnz={lu.stats['lu_nnz']} fill={lu.stats['fill']}")


def scrambled(matrix, seed=99):
    """对行/列标签做固定随机对称置换，模拟“自然序很糟糕”的真实矩阵。"""
    import random
    perm = list(range(matrix.n))
    random.Random(seed).shuffle(perm)
    return matrix.permute_symmetric(perm)


def test_diagonal():
    a = diagonal_matrix(500)
    lu = factor(a, block_size=32)
    check("对角矩阵零填充", lu.stats["fill"] == 0 and lu.stats["lu_nnz"] == 500,
          f"LU nnz={lu.stats['lu_nnz']}")
    res = factor_residual(lu, a)
    check("对角矩阵残差为 0", res == 0.0, f"res={res}")


def test_banded_envelope():
    bw = 4
    a = banded_matrix(300, bw, seed=7)
    for order_name, ofn in [("natural", natural_order), ("rcm", rcm_order)]:
        perm = ofn(a) if order_name != "natural" else natural_order(a.n)
        ap = a.permute_symmetric(perm)
        lu = factor(ap, block_size=16)
        # 无选主元 LU 的填充不超出半带宽包络：|i-j| <= bw
        outside = 0
        for p in lu.panels:
            for i, pl in p["L"].items():
                outside += sum(1 for k, _ in pl if i - k > bw)
            for i, ui in p["U"].items():
                outside += sum(1 for j in ui if j - i > bw)
        check(f"带状矩阵({order_name})填充不越出带宽包络", outside == 0,
              f"越界条目={outside}, fill={lu.stats['fill']}")


def test_symbolic_matches_numeric():
    cases = {
        "banded500_bw8": banded_matrix(500, 8, seed=1),
        "random400": random_sparse_matrix(400, 2000, seed=2),
        "poisson20x20": poisson_2d_matrix(20, 20),
        "neardense80": near_dense_matrix(80, 0.6, seed=3),
    }
    for name, a in cases.items():
        sym = symbolic_lu(a)
        lu = factor(a, block_size=24)
        ok = (sym.l_nnz == lu.stats["l_nnz"] and sym.u_nnz == lu.stats["u_nnz"])
        check(f"符号分解填充数==数值分解: {name}", ok,
              f"符号 L={sym.l_nnz} U={sym.u_nnz}; 数值 L={lu.stats['l_nnz']} U={lu.stats['u_nnz']}")


def test_rcm_reduces_fill():
    # 标签打乱的网格：自然序带宽爆炸，RCM/最小度恢复结构
    a = scrambled(poisson_2d_matrix(30, 30))
    s_nat = symbolic_lu(a)
    s_rcm = symbolic_lu(a.permute_symmetric(rcm_order(a)))
    s_md = symbolic_lu(a.permute_symmetric(min_degree_order(a)))
    print(f"    打乱 Poisson 30x30: A nnz={a.nnz}  natural LU={s_nat.lu_nnz} fill={s_nat.fill}; "
          f"RCM LU={s_rcm.lu_nnz} fill={s_rcm.fill}; min-degree LU={s_md.lu_nnz} fill={s_md.fill}")
    check("RCM 相比自然序显著减少填充", s_rcm.fill < s_nat.fill * 0.3,
          f"natural fill={s_nat.fill}, rcm fill={s_rcm.fill}")
    check("最小度填充不劣于 RCM(量级相当或更优)", s_md.fill <= s_rcm.fill * 1.5,
          f"rcm={s_rcm.fill}, mindeg={s_md.fill}")


def test_memory_guard_refuses():
    # 极小预算：应抛 MemoryBudgetExceeded 而不是被系统杀掉
    a = banded_matrix(2000, 30, seed=4)
    base = current_rss_bytes() or 0
    raised = None
    try:
        factor(a, block_size=16, memory_limit_bytes=base + 4 * 1024 * 1024)
    except MemoryBudgetExceeded as exc:
        raised = exc
    check("内存超限时明确拒绝(异常)", raised is not None,
          str(raised)[:120] + "..." if raised else "未触发")
    # 进程仍然存活，后续分解在正常预算下可以继续
    lu = factor(a, block_size=16, memory_limit_bytes=512 * 1024 * 1024)
    check("拒绝后进程存活可继续工作", lu.stats["u_nnz"] > 0)


def test_symbolic_guard():
    a = random_sparse_matrix(2000, 20000, seed=5)
    base = current_rss_bytes() or 0
    raised = False
    try:
        symbolic_lu(a, guard=MemoryGuard(base + 8 * 1024 * 1024), check_interval=128)
    except MemoryBudgetExceeded:
        raised = True
    check("符号分解同样受内存守卫保护", raised)


def test_spill():
    a = banded_matrix(600, 10, seed=8)
    tmp = tempfile.mkdtemp(prefix="sparse_lu_spill_")
    lu = factor(a, block_size=32, spill_dir=tmp)
    files = [f for f in os.listdir(tmp) if f.endswith(".pkl")]
    check("溢写：面板文件已落盘", len(files) == lu.stats["panels"],
          f"{len(files)} 个文件 / {lu.stats['panels']} 面板")
    # 内存中不保留任何 L/U 条目
    in_ram = sum(len(pl) + len(ui) for p in lu.panels if "L" in p
                 for pl in list(p["L"].values()) + list(p["U"].values()))
    check("溢写：L/U 不再驻留内存", in_ram == 0, f"驻留条目={in_ram}")
    bvec = [math.sin(i * 0.01) + 1.0 for i in range(a.n)]
    x = lu.solve(bvec)
    res = relative_residual(a, x, bvec)
    check("溢写分解求解残差小", res < 1e-9, f"相对残差={res:.3e}")
    resf = factor_residual(lu, a)
    check("溢写分解因子残差小", resf < 1e-10, f"||A-LU||/||A||={resf:.3e}")


def test_refinement_history():
    # slack 很小 => 弱对角占优、条件数大，初始 LU 解残差较高，需要多轮精化
    a = random_sparse_matrix(300, 1500, seed=9, slack=1e-4)
    lu = factor(a, block_size=20)
    bvec = [1.0 + 0.001 * i for i in range(a.n)]
    x, hist = iterative_refinement(a, lu, bvec, max_iter=8, tol=1e-12)
    monotone = all(hist[i + 1][1] <= hist[i][1] * 1.01 + 1e-30
                   for i in range(len(hist) - 1))
    converged = hist[-1][1] <= 1e-12
    print("    迭代精化残差: " + " ".join(f"it={it}:{r:.2e}" for it, r in hist))
    check("迭代精化残差单调下降", monotone)
    check("迭代精化在限次内收敛", converged, f"末次残差={hist[-1][1]:.3e} 迭代次数={hist[-1][0]}")


def test_solve_correctness():
    a = poisson_2d_matrix(15, 12)
    lu = factor(a, block_size=10)
    # 制造 b = A x_true
    x_true = [math.cos(i * 0.07) for i in range(a.n)]
    bvec = a.matvec(x_true)
    x = lu.solve(bvec)
    err = max(abs(xi - ti) for xi, ti in zip(x, x_true))
    scale = max(abs(t) for t in x_true)
    check("线性求解正确性 (已知真解)", err / scale < 1e-9, f"相对误差={err/scale:.3e}")


def test_zero_pivot_message():
    rows = [{1: 2.0}, {0: 3.0}]  # 无对角元
    a = SparseMatrix(2, rows)
    raised = False
    try:
        factor(a)
    except ZeroPivotError:
        raised = True
    check("零主元明确报错", raised)


def main():
    t0 = time.time()
    print("== 1. 各类矩阵对稠密参考的一致性 ==")
    test_diagonal()
    test_vs_dense("带状 B=1", banded_matrix(60, 4, seed=11), lambda a: natural_order(a.n), 1)
    test_vs_dense("带状", banded_matrix(60, 4, seed=11), lambda a: natural_order(a.n), 16)
    test_vs_dense("随机稀疏+natural", random_sparse_matrix(50, 250, seed=12),
                  lambda a: natural_order(a.n), 7)
    test_vs_dense("随机稀疏+rcm", random_sparse_matrix(50, 250, seed=12), rcm_order, 7)
    test_vs_dense("随机稀疏+mindeg", random_sparse_matrix(50, 250, seed=12), min_degree_order, 7)
    test_vs_dense("Poisson+rcm", poisson_2d_matrix(8, 6), rcm_order, 5)
    test_vs_dense("近稠密", near_dense_matrix(40, 0.9, seed=13),
                  lambda a: natural_order(a.n), 16)

    print("== 2. 填充规则 / 符号分解 ==")
    test_banded_envelope()
    test_symbolic_matches_numeric()
    test_rcm_reduces_fill()

    print("== 3. 内存超限处理 ==")
    test_memory_guard_refuses()
    test_symbolic_guard()

    print("== 4. 溢写分解 ==")
    test_spill()

    print("== 5. 求解与迭代精化 ==")
    test_solve_correctness()
    test_refinement_history()
    test_zero_pivot_message()

    print(f"\n用时 {time.time() - t0:.2f}s")
    if FAILURES:
        print(f"{len(FAILURES)} 项失败: {FAILURES}")
        raise SystemExit(1)
    print("全部自测通过。")


if __name__ == "__main__":
    main()

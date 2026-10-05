"""跑全部实验并把数据写入 results/。

用法：
  python3 run_experiments.py              # 全部实验（含 1 万变量大图，约 1-2 分钟）
  python3 run_experiments.py --quick      # 跳过 1 万变量大图
"""

import argparse
import csv
import math
import os
import random
import resource
import time

from pgmbp import BPConfig, belief_propagation, exact_marginals
from pgmbp import models

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def peak_rss_mb():
    """进程峰值常驻内存（MB）。Linux ru_maxrss 单位为 KB。"""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def errors_vs_exact(graph, beliefs):
    exact = exact_marginals(graph)
    max_abs = 0.0
    total_abs = 0.0
    n = 0
    worst = None
    for v, (b, e) in enumerate(zip(beliefs, exact)):
        for x, (bpv, ev) in enumerate(zip(b, e)):
            d = abs(bpv - ev)
            total_abs += d
            n += 1
            if d > max_abs:
                max_abs = d
                worst = (v, x, bpv, ev)
    return max_abs, total_abs / n, exact, worst


def save_residual_csv(name, residuals):
    path = os.path.join(RESULTS_DIR, name + "_residuals.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["iteration", "residual"])
        for i, r in enumerate(residuals, 1):
            w.writerow([i, "%.6e" % r])
    return path


def ascii_plot(points, width=64, height=12, title=""):
    """对数纵坐标残差曲线（纯文本）。points: (iter, residual)。"""
    if not points:
        return "(无数据)"
    xs = [p[0] for p in points]
    ys = [max(p[1], 1e-16) for p in points]
    log_ys = [math.log10(y) for y in ys]
    ymin, ymax = min(log_ys), max(log_ys)
    if ymax - ymin < 1e-9:
        ymax = ymin + 1.0
    grid = [[" "] * width for _ in range(height)]
    n = len(points)
    for idx, (x, ly) in enumerate(zip(xs, log_ys)):
        col = int((x - xs[0]) / max(xs[-1] - xs[0], 1) * (width - 1))
        row = int((ymax - ly) / (ymax - ymin) * (height - 1))
        row = min(max(row, 0), height - 1)
        grid[row][col] = "*"
    lines = []
    if title:
        lines.append(title)
    lines.append("  1e%+.0f %s" % (ymax, "| " + "".join(grid[0])))
    for r in range(1, height):
        lab = "      " if r != height - 1 else "  1e%+.0f " % ymin
        lines.append(lab + "| " + "".join(grid[r]))
    lines.append("        +" + "-" * width)
    lines.append("        %-*d%*d" % (width // 2, xs[0], width - width // 2, xs[-1]))
    lines.append("        " + " " * (width // 2 - 3) + "iteration")
    return "\n".join(lines)


def run_case(name, graph, config, compare_exact=True):
    print("== %s ==" % name, flush=True)
    t0 = time.perf_counter()
    res = belief_propagation(graph, config)
    elapsed = time.perf_counter() - t0
    save_residual_csv(name, res.residuals)
    row = {
        "case": name,
        "status": res.status,
        "iterations": res.iterations,
        "final_residual": res.final_residual,
        "min_residual": min(res.residuals),
        "damping": res.damping_used,
        "degenerate_messages": res.degenerate_messages,
        "time_sec": elapsed,
    }
    worst_str = ""
    if compare_exact:
        try:
            max_abs, mean_abs, exact, worst = errors_vs_exact(graph, res.beliefs)
            row["max_abs_err"] = max_abs
            row["mean_abs_err"] = mean_abs
            if worst is not None:
                v, x, bpv, ev = worst
                worst_str = "  最大误差: 变量%d取值%d BP=%.6f 精确=%.6f" % (
                    v, x, bpv, ev
                )
        except ArithmeticError as e:
            row["max_abs_err"] = "N/A(模型不可满足)"
            row["mean_abs_err"] = "N/A"
    print(
        "  status=%s iters=%d final_r=%.3e time=%.3fs damp=%.1f deg=%d%s"
        % (
            res.status,
            res.iterations,
            res.final_residual,
            elapsed,
            res.damping_used,
            res.degenerate_messages,
            worst_str,
        ),
        flush=True,
    )
    return row, res


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    rows = []

    # 1) 树结构（链）：BP 理论上应精确
    g = models.chain_mrf(10, random.Random(7))
    row, _ = run_case("tree_chain", g, BPConfig(tol=1e-10))
    rows.append(row)

    # 2) 含环结构（3x3 网格）：BP 近似，与 VE 对比
    g = models.grid_mrf(3, 3, random.Random(3))
    row, _ = run_case("loopy_grid", g, BPConfig(tol=1e-10))
    rows.append(row)

    # 3) 强耦合不收敛：无阻尼振荡
    g = models.ising_grid(5, 5, beta=1.0, rng=random.Random(5), frustrated=False)
    row_osc, res_osc = run_case(
        "strong_coupling_nodamp",
        g,
        BPConfig(tol=1e-9, max_iter=300, damping=0.0, adaptive_damping=False),
    )
    rows.append(row_osc)

    # 4) 同一强耦合图：阻尼恢复收敛
    g = models.ising_grid(5, 5, beta=1.0, rng=random.Random(5), frustrated=False)
    row_damp, res_damp = run_case(
        "strong_coupling_damped",
        g,
        BPConfig(tol=1e-9, max_iter=2000, damping=0.5, adaptive_damping=False),
    )
    rows.append(row_damp)

    # 5) 消息退化：确定性因子产生零分量消息
    g = models.degenerate_zero_graph()
    row, _ = run_case("degenerate_zeros", g, BPConfig(tol=1e-10))
    rows.append(row)

    # 6) 消息整条约零（矛盾约束）：防护回退
    g = models.degenerate_contradiction_graph()
    row, res_contra = run_case(
        "degenerate_contradiction",
        g,
        BPConfig(tol=1e-10, max_iter=200),
        compare_exact=False,
    )
    rows.append(row)

    # 汇总写 CSV
    fields = [
        "case",
        "status",
        "iterations",
        "final_residual",
        "min_residual",
        "damping",
        "degenerate_messages",
        "max_abs_err",
        "mean_abs_err",
        "time_sec",
    ]
    with open(os.path.join(RESULTS_DIR, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})

    # ASCII 收敛曲线
    plots = []
    for name, label in [
        ("tree_chain", "tree_chain (树/链)"),
        ("loopy_grid", "loopy_grid (含环网格)"),
        ("strong_coupling_nodamp", "strong_coupling_nodamp (无阻尼)"),
        ("strong_coupling_damped", "strong_coupling_damped (阻尼0.5)"),
        ("degenerate_zeros", "degenerate_zeros (零分量消息)"),
    ]:
        with open(os.path.join(RESULTS_DIR, name + "_residuals.csv")) as f:
            pts = [(int(r["iteration"]), float(r["residual"]))
                   for r in csv.DictReader(f)]
        plots.append(ascii_plot(pts, title="residual curve: " + label))
    with open(os.path.join(RESULTS_DIR, "convergence_curves.txt"), "w") as f:
        f.write("\n\n".join(plots) + "\n")

    # 振荡样例的具体输出
    with open(os.path.join(RESULTS_DIR, "oscillation_tail.txt"), "w") as f:
        f.write("strong_coupling_nodamp: status=%s\n" % res_osc.status)
        f.write("末 12 轮残差:\n")
        for i, r in enumerate(res_osc.residuals[-12:],
                              res_osc.iterations - 11):
            f.write("  iter %3d  residual=%.10f\n" % (i, r))
        f.write(
            "处置：标记 status=oscillating，返回历史残差最小一轮的边缘；"
            "同图加阻尼 0.5 后 %s（%d 轮）。\n"
            % (res_damp.status, res_damp.iterations)
        )
        f.write("振荡样例返回的（best-effort）前 3 个变量边缘:\n")
        for v in range(3):
            f.write("  x%d: %s\n" % (v, res_osc.beliefs[v]))
        f.write("阻尼收敛后的精确边缘:\n")
        dmax, dmean, exact, _ = errors_vs_exact(
            models.ising_grid(5, 5, beta=1.0, rng=random.Random(5),
                              frustrated=False),
            res_damp.beliefs,
        )
        for v in range(3):
            f.write("  x%d: BP=%s exact=%s\n"
                    % (v, res_damp.beliefs[v], exact[v]))
        f.write("阻尼收敛边缘最大误差=%.3e 平均误差=%.3e\n" % (dmax, dmean))
        f.write("\n矛盾约束样例（degenerate_contradiction）:\n")
        f.write("  status=%s degenerate_messages=%d\n"
                % (res_contra.status, res_contra.degenerate_messages))
        for v, b in enumerate(res_contra.beliefs):
            f.write("  x%d 边缘=%s（防护回退，非真实后验，调用方不应采信）\n"
                    % (v, b))
        f.write("  warnings: %s\n" % " | ".join(res_contra.warnings[:5]))

    if not args.quick:
        # 7) 大规模：1 万变量、10 万边
        print("== large_random (10k vars / 100k edges) ==", flush=True)
        rng = random.Random(42)
        t_build0 = time.perf_counter()
        g = models.random_graph_mrf(10000, 100000, rng, coupling_scale=0.25)
        t_build = time.perf_counter() - t_build0
        rss_after_build = peak_rss_mb()
        t0 = time.perf_counter()
        res = belief_propagation(g, BPConfig(tol=1e-9, max_iter=1000))
        t_infer = time.perf_counter() - t0
        save_residual_csv("large_random", res.residuals)
        rss_peak = peak_rss_mb()
        valid = all(
            math.isfinite(p) and p >= 0.0 and abs(sum(b) - 1.0) < 1e-8
            for b in res.beliefs for p in b
        )
        with open(os.path.join(RESULTS_DIR, "large_scale.txt"), "w") as f:
            f.write("variables: %d\n" % g.num_vars)
            f.write("edges(pairwise): %d\n" % g.num_edges())
            f.write("factors: %d (10000 一元 + 100000 成对)\n"
                    % g.num_factors)
            f.write("build_time_sec: %.3f\n" % t_build)
            f.write("inference_time_sec: %.3f\n" % t_infer)
            f.write("sec_per_iteration: %.3f\n"
                    % (t_infer / max(res.iterations, 1)))
            f.write("total_time_sec: %.3f\n" % (t_build + t_infer))
            f.write("iterations: %d\n" % res.iterations)
            f.write("status: %s\n" % res.status)
            f.write("final_residual: %.6e\n" % res.final_residual)
            f.write("rss_after_build_mb: %.1f\n" % rss_after_build)
            f.write("peak_rss_mb: %.1f\n" % rss_peak)
            f.write("all_beliefs_valid: %s\n" % valid)
        print(
            "  status=%s iters=%d infer=%.1fs build=%.2fs peak_rss=%.0fMB valid=%s"
            % (res.status, res.iterations, t_infer, t_build, rss_peak, valid),
            flush=True,
        )

    print("\n结果已写入 %s" % RESULTS_DIR, flush=True)


if __name__ == "__main__":
    main()

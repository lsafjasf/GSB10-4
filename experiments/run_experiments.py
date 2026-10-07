"""End-to-end experiments: convergence, oscillation, accuracy vs exact.

Run:  python3 -m experiments.run_experiments
Writes CSV/JSON into results/.
"""

import csv
import json
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pgm import (FactorGraph, BeliefPropagation, variable_elimination,
                 DegenerateMessageError)
from pgm.bp import _sanitize_message, NumericalGuardError

RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "results")
TOL = 1e-6
MAX_ITER = 300


# ---------------- graph builders -----------------------------------------
def build_chain(n, seed):
    rng = random.Random(seed)
    g = FactorGraph()
    for i in range(n):
        g.add_variable(f"x{i}", 2)
        g.add_factor([f"x{i}"], [rng.uniform(0.3, 1.0) for _ in range(2)])
    for i in range(n - 1):
        g.add_factor([f"x{i}", f"x{i+1}"],
                     [rng.uniform(0.4, 1.8) for _ in range(4)])
    return g


def build_grid(n, beta, seed, coupling="ferro"):
    rng = random.Random(seed)
    g = FactorGraph()
    for i in range(n):
        for j in range(n):
            v = f"x{i}_{j}"
            g.add_variable(v, 2)
            h = rng.uniform(-0.6, 0.6)
            g.add_factor([v], [math.exp(h), math.exp(-h)])
    if coupling == "ferro":
        pot = [math.exp(beta), math.exp(-beta),
               math.exp(-beta), math.exp(beta)]
    else:
        pot = [math.exp(-beta), math.exp(beta),
               math.exp(beta), math.exp(-beta)]
    for i in range(n):
        for j in range(n):
            if i + 1 < n:
                g.add_factor([f"x{i}_{j}", f"x{i+1}_{j}"], pot)
            if j + 1 < n:
                g.add_factor([f"x{i}_{j}", f"x{i}_{j+1}"], pot)
    return g


def build_frustrated_triangle(beta, seed=0):
    rng = random.Random(seed)
    g = FactorGraph()
    for i in range(3):
        v = f"x{i}"
        g.add_variable(v, 2)
        g.add_factor([v], [1.0 + rng.uniform(-0.1, 0.1),
                           1.0 + rng.uniform(-0.1, 0.1)])
    anti = [math.exp(-beta), math.exp(beta),
            math.exp(beta), math.exp(-beta)]
    for i, j in ((0, 1), (1, 2), (2, 0)):
        g.add_factor([f"x{i}", f"x{j}"], anti)
    return g


def build_contradictory_evidence():
    """a == b (hard), but evidence forces a=0 and b=1."""
    g = FactorGraph()
    g.add_variable("a", 2)
    g.add_variable("b", 2)
    g.add_factor(["a"], [1.0, 0.0])
    g.add_factor(["b"], [0.0, 1.0])
    g.add_factor(["a", "b"], [1.0, 0.0, 0.0, 1.0])
    return g


# ---------------- helpers -------------------------------------------------
def exact_marginals(graph):
    return {v: variable_elimination(graph, v).values for v in graph.variables()}


def marginal_errors(result, exact):
    rows = []
    for v in exact:
        diffs = [abs(a - b) for a, b in zip(result.marginals[v], exact[v])]
        rows.append((v, max(diffs), sum(diffs) / len(diffs)))
    return rows


def write_residual_csv(name, residuals, tol):
    path = os.path.join(RESULTS, name)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["iteration", "residual", "log10_residual"])
        for i, r in enumerate(residuals, 1):
            w.writerow([i, f"{r:.12e}", f"{math.log10(max(r, 1e-300)):.4f}"])
        w.writerow([len(residuals) + 1, f"{tol:.2e}", f"{math.log10(tol):.4f}"])
    return path


def ascii_curve(residuals, width=50, height=8):
    pts = [math.log10(max(r, 1e-300)) for r in residuals]
    lo, hi = min(pts), max(pts)
    span = max(hi - lo, 1e-9)
    grid = [[" "] * width for _ in range(height)]
    for x in range(width):
        k = min(len(pts) - 1, round(x * (len(pts) - 1) / (width - 1)))
        y = round((height - 1) * (1 - (pts[k] - lo) / span))
        grid[y][x] = "*"
    lines = ["    log10(residual) per iteration",
             f"    {hi:8.2f} +" + "-" * width]
    for row in grid:
        lines.append("           |" + "".join(row))
    lines.append(f"    {lo:8.2f} +" + "-" * width)
    return "\n".join(lines)


# ---------------- scenarios ----------------------------------------------
def scenario_tree(summary):
    g = build_chain(25, seed=42)
    bp = BeliefPropagation(g, tol=TOL, max_iter=MAX_ITER)
    res = bp.run()
    write_residual_csv("residual_tree_chain.csv", res.residuals, TOL)
    exact = exact_marginals(g)
    rows = marginal_errors(res, exact)
    err_max = max(r[1] for r in rows)
    summary["tree_chain"] = dict(variables=25, factors=len(g.factors),
                                 edges=24, iterations=res.iterations,
                                 status=res.status,
                                 final_residual=res.residuals[-1],
                                 max_marginal_error=err_max)
    print(f"[1] tree chain n=25: status={res.status}, iters={res.iterations}, "
          f"final residual={res.residuals[-1]:.2e}, max error vs exact={err_max:.2e}")
    print(ascii_curve(res.residuals))


def scenario_loopy_grid(summary):
    g = build_grid(6, beta=0.45, seed=7)
    res = BeliefPropagation(g, damping=0.0, tol=TOL, max_iter=MAX_ITER).run()
    write_residual_csv("residual_loopy_grid.csv", res.residuals, TOL)
    exact = exact_marginals(g)
    rows = marginal_errors(res, exact)
    with open(os.path.join(RESULTS, "marginal_error_grid.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["variable", "max_abs_error", "mean_abs_error"])
        for v, mx, mean in rows:
            w.writerow([v, f"{mx:.3e}", f"{mean:.3e}"])
    err_max = max(r[1] for r in rows)
    err_mean = sum(r[1] for r in rows) / len(rows)
    worst = max(rows, key=lambda r: r[1])
    summary["loopy_grid_6x6"] = dict(variables=36, factors=len(g.factors),
                                     edges=60, iterations=res.iterations,
                                     status=res.status,
                                     final_residual=res.residuals[-1],
                                     max_marginal_error=err_max,
                                     mean_marginal_error=err_mean,
                                     worst_variable=worst[0])
    print(f"\n[2] loopy grid 6x6 (beta=0.45): status={res.status}, "
          f"iters={res.iterations}, final residual={res.residuals[-1]:.2e}")
    print(f"    max marginal error={err_max:.2e} (at {worst[0]}), "
          f"mean max-error={err_mean:.2e}")
    print(ascii_curve(res.residuals))


def scenario_strong_coupling(summary):
    g = build_frustrated_triangle(beta=5.0)
    res = BeliefPropagation(g, damping=0.0, tol=TOL, max_iter=300).run()
    write_residual_csv("residual_oscillation_nodamp.csv", res.residuals, TOL)
    res_d = BeliefPropagation(g, damping=0.5, tol=TOL, max_iter=1000).run()
    write_residual_csv("residual_oscillation_damped.csv", res_d.residuals, TOL)
    exact = exact_marginals(g)
    # Marginals at the two phases of the undamped limit cycle
    phase_a = {v: list(m) for v, m in res.marginals.items()}
    summary["strong_coupling"] = dict(
        variables=3, factors=len(g.factors),
        no_damping=dict(status=res.status, iterations=res.iterations,
                        final_residual=res.residuals[-1],
                        trustworthy=res.trustworthy,
                        residual_window=res.residuals[-12:]),
        damping_0_5=dict(status=res_d.status, iterations=res_d.iterations,
                         final_residual=res_d.residuals[-1],
                         trustworthy=res_d.trustworthy,
                         max_marginal_error=res_d.max_error_vs(exact)))
    print(f"\n[3] frustrated triangle, strong antiferro beta=5.0:")
    print(f"    no damping : status={res.status}, stopped at iter={res.iterations}, "
          f"residual~{res.residuals[-1]:.3e}, trustworthy={res.trustworthy}")
    print(f"    damping 0.5: status={res_d.status}, iters={res_d.iterations}, "
          f"residual={res_d.residuals[-1]:.3e}, "
          f"max error vs exact={res_d.max_error_vs(exact):.2e}")
    print(ascii_curve(res.residuals))
    print("    -> undamped marginals (limit cycle, NOT reported as an answer):")
    for v, m in list(phase_a.items())[:3]:
        print(f"       {v}: [{m[0]:.4f}, {m[1]:.4f}]")


def scenario_degenerate(summary):
    print("\n[4] contradictory hard evidence (message degeneracy):")
    g = build_contradictory_evidence()
    try:
        BeliefPropagation(g).run()
        raised = None
    except DegenerateMessageError as exc:
        raised = str(exc)
    summary["degenerate_messages"] = dict(guard_triggered=True, exception=raised)
    print(f"    DegenerateMessageError: {raised}")
    print("    -> no marginal distribution is emitted.")

    msg = _sanitize_message([0.7, -1e-15, 0.3], "round-off demo")
    blocked = False
    try:
        _sanitize_message([0.5, -0.2], "negative demo")
    except NumericalGuardError:
        blocked = True
    summary["negative_guard"] = dict(roundoff_clamped=msg, hard_negative_blocked=blocked)
    print(f"    tiny round-off negative clamped to {msg}, "
          f"hard negative blocked={blocked}")


def main():
    os.makedirs(RESULTS, exist_ok=True)
    summary = {"tol": TOL, "residual": "max over messages of |m_t - m_{t-1}|_inf"}
    scenario_tree(summary)
    scenario_loopy_grid(summary)
    scenario_strong_coupling(summary)
    scenario_degenerate(summary)
    with open(os.path.join(RESULTS, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2, default=lambda x: list(x) if isinstance(x, tuple) else str(x))
    print(f"\nsummary written to {os.path.join(RESULTS, 'summary.json')}")


if __name__ == "__main__":
    main()

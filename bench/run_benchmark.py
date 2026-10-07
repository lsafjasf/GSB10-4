"""大规模基准：10,000 条约束 / 1,200 个变量。

构造：
  * 背景约束 10,000 条，按随机势函数 p 生成 c = p[u]-p[v]+slack(slack>=1)，
    保证背景本身可满足且留有 1..20 的松弛；
  * 注入 6 个互不相交的 5 环，环上前 4 条边贴紧背景（slack=0），
    最后一条边权 = 精确和 - 1，使每环总权恰为 -1 —— 6 个彼此独立的 MUS；
  * 松弛背景保证不会产生计划外的负环（背景图本身无负环，注入边按
    精确和-1 取值，任何含注入边的环若含 slack>=1 的背景边则总权 >= 0）。

度量：各阶段 wall-time（perf_counter）与内存峰值
（tracemalloc Python 堆峰值 + resource.ru_maxrss 进程峰值）。

运行：python3 bench/run_benchmark.py
"""

import json
import os
import random
import resource
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conflict_locator import (Constraint, DifferenceSystem, diagnose,
                              enumerate_mus, find_one_mus, find_some_mus,
                              verify_mus)

SEED = 20261007
N_VARS = 1200
N_BACKGROUND = 10_000
N_CYCLES = 6
CYCLE_LEN = 5


def gen_background(rng, n_vars, m):
    potential = [rng.randint(-10_000, 10_000) for _ in range(n_vars + 1)]
    cons = []
    for i in range(m):
        u = rng.randint(1, n_vars)
        v = rng.randint(1, n_vars)
        slack = rng.randint(1, 20)
        c = potential[u] - potential[v] + slack
        cons.append(Constraint(u, v, c, f"B{i+1}"))
    return cons, potential


def inject_cycles(rng, cons, potential, n_vars):
    used = set()
    injected = []
    for k in range(N_CYCLES):
        while True:
            vs = rng.sample(range(1, n_vars + 1), CYCLE_LEN)
            if not any(v in used for v in vs):
                used.update(vs)
                break
        # 前 L-1 条边贴紧背景: vs[i] -> vs[i+1], c = p[u]-p[v]
        for i in range(CYCLE_LEN - 1):
            a, b = vs[i], vs[i + 1]
            cons.append(Constraint(b, a, potential[b] - potential[a],
                                   f"X{k+1}_{i+1}"))
        # 最后一条: vs[L-1] -> vs[0], c = 精确和 - 1 => 环总权 -1
        a, b = vs[-1], vs[0]
        cons.append(Constraint(b, a, potential[b] - potential[a] - 1,
                               f"X{k+1}_{CYCLE_LEN}"))
        injected.append(frozenset(f"X{k+1}_{i+1}" for i in range(CYCLE_LEN)))
    return injected


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main():
    rng = random.Random(SEED)
    t0 = time.perf_counter()
    background, potential = gen_background(rng, N_VARS, N_BACKGROUND)
    sat_cons = list(background)
    unsat_cons = list(background)
    injected = inject_cycles(rng, unsat_cons, potential, N_VARS)
    t_gen = time.perf_counter() - t0
    print(f"[构造] 背景 {len(background)} 条 + 注入 {N_CYCLES*CYCLE_LEN} 条 "
          f"= {len(unsat_cons)} 条约束, {N_VARS} 变量, 耗时 {t_gen:.3f}s")

    # ---- 可满足实例 ----
    t0 = time.perf_counter()
    sys_sat = DifferenceSystem(N_VARS, sat_cons)
    res_sat = sys_sat.check()
    t_sat = time.perf_counter() - t0
    assert res_sat.feasible
    print(f"[SAT 判定] 可满足={res_sat.feasible}, "
          f"Bellman-Ford 轮数={res_sat.stats['passes']}, 耗时 {t_sat:.3f}s")

    # ---- 不可满足实例：建图 + 判定 ----
    t0 = time.perf_counter()
    sys_unsat = DifferenceSystem(N_VARS, unsat_cons)
    t_build = time.perf_counter() - t0
    print(f"[建图] {len(unsat_cons)} 条约束, 耗时 {t_build:.3f}s")

    tracemalloc.start()
    t0 = time.perf_counter()
    res_unsat = sys_unsat.check()
    t_check = time.perf_counter() - t0
    assert not res_unsat.feasible
    print(f"[UNSAT 判定] 可满足={res_unsat.feasible}, "
          f"轮数={res_unsat.stats['passes']}, "
          f"提前检环={res_unsat.stats['early_cycle']}, 耗时 {t_check:.3f}s")

    # ---- 提取一个 MUS ----
    t0 = time.perf_counter()
    one = find_one_mus(unsat_cons)
    t_one = time.perf_counter() - t0
    print(f"[提取1个MUS] {one.cids}, 总权={one.cycle_weight}, "
          f"最小性已验证={one.verified_minimal}, 耗时 {t_one:.3f}s")

    # ---- 快速枚举若干 MUS（线性预算）----
    t0 = time.perf_counter()
    some, calls_some = find_some_mus(unsat_cons, max_mus=N_CYCLES)
    t_some = time.perf_counter() - t0
    got_some = {frozenset(mc.cids) for mc in some}
    print(f"[线性枚举] 找到 {len(some)} 个 MUS, 求解调用 {calls_some} 次, "
          f"命中全部注入环={got_some == set(injected)}, 耗时 {t_some:.3f}s")

    # ---- 最小性验证（全部 MUS，逐条删除复核）----
    t0 = time.perf_counter()
    n_checks = 0
    all_ok = True
    verify_export = []
    for mc in some:
        ok, checks = verify_mus(sys_unsat, mc.cids)
        n_checks += len(checks) + 1
        all_ok = all_ok and ok
        verify_export.append({
            "cids": list(mc.cids),
            "cycle_weight": mc.cycle_weight,
            "verified_minimal": ok,
            "deletion_checks": [
                {"removed": cid, "remainder_satisfiable": sat}
                for cid, sat in checks
            ],
        })
    t_verify = time.perf_counter() - t0
    out_json = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "verification_benchmark.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"n_vars": N_VARS,
                   "n_constraints": len(unsat_cons),
                   "mus_count": len(some),
                   "all_verified_minimal": all_ok,
                   "conflicts": verify_export}, f, ensure_ascii=False, indent=2)
    print(f"[最小性验证] {len(some)} 个 MUS 共 {n_checks} 次独立判定, "
          f"全部通过={all_ok}, 耗时 {t_verify:.3f}s (数据见 {out_json})")

    # ---- 分支枚举（max_mus=7 > 真实 MUS 数 6，预算 100，演示预算取舍）----
    # 6 个互不相交的环使"完整枚举"必须探索所有击破组合（最坏 5^6 量级），
    # 预算耗尽时如实在报告中标 complete=False；小规模实例的完整性由自测保证。
    t0 = time.perf_counter()
    many, complete, calls_many = enumerate_mus(unsat_cons, max_mus=N_CYCLES + 1,
                                               max_calls=100, verify=False)
    t_enum = time.perf_counter() - t0
    print(f"[分支枚举] 预算 100 次求解内找到 {len(many)} 个 MUS, "
          f"实际调用 {calls_many} 次, 枚举完整={complete}, 耗时 {t_enum:.3f}s")

    # ---- 最坏情况构造：长链传播需要 n 轮（无提前终止），惰性自环补足 1 万边 ----
    hard = [Constraint(i, i + 1, -1, f"H{i}") for i in range(1, N_VARS)]
    hard.append(Constraint(1, N_VARS, N_VARS, "Hclose"))  # 闭合正边，系统仍可满足
    hard += [Constraint(v, v, 0, f"P{j}") for j, v in
             enumerate(rng.choices(range(1, N_VARS + 1), k=N_BACKGROUND - N_VARS))]
    t0 = time.perf_counter()
    res_hard = DifferenceSystem(N_VARS, hard).check()
    t_hard = time.perf_counter() - t0
    assert res_hard.feasible
    print(f"[最坏单调用] {len(hard)} 条约束, 链长 {N_VARS} 需 "
          f"{res_hard.stats['passes']} 轮（达到 O(nm) 上界）, 耗时 {t_hard:.3f}s")

    # ---- 完整诊断（SAT / UNSAT 各一次）----
    t0 = time.perf_counter()
    diag_u = diagnose(unsat_cons, max_mus=N_CYCLES, max_calls=40)
    diag_s = diagnose(sat_cons, max_mus=N_CYCLES, max_calls=40)
    t_diag = time.perf_counter() - t0
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"[完整诊断] UNSAT 报告 MUS 数={len(diag_u.conflicts)}, "
          f"SAT 报告状态={diag_s.status}, 耗时 {t_diag:.3f}s")

    print("-" * 56)
    print(f"Python 堆峰值(tracemalloc): {peak/1024/1024:.1f} MiB")
    print(f"进程峰值 RSS(ru_maxrss):    {rss_mb():.1f} MiB (含解释器)")
    solve_total = (t_sat + t_build + t_check + t_one + t_some +
                   t_verify + t_enum + t_hard + t_diag)
    print(f"求解侧总耗时(含最坏单调用, 不含数据生成): {solve_total:.3f}s")


if __name__ == "__main__":
    main()

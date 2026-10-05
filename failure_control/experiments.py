"""大批次实验：实测失败率 vs 理论推导 2^-k，含 Wilson 置信区间。

运行：python3 experiments.py
产物：artifacts/ 下的对拍数据、复现数据与 experiment_summary.json
"""

import csv
import hashlib
import json
import math
import os
import resource
import time
import tracemalloc

import freivalds as F

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
os.makedirs(ART, exist_ok=True)

N = 8  # 矩阵维度


def eye(n):
    return [[1 if i == j else 0 for j in range(n)] for i in range(n)]


def rank1_witness(n=N):
    """最坏情形构造：AB 与 C 仅差一个元素，D=AB⊕C 秩 1，单次误判率恰为 1/2。"""
    a, b = eye(n), eye(n)
    c = [row[:] for row in eye(n)]
    c[0][0] ^= 1
    return a, b, c


def wilson_ci(fails, total, z=1.959964):
    """Wilson score 区间（95%）；fails=0 时等价于 Clopper-Pearson 上界 1-α^(1/n)。"""
    if total == 0:
        return (0.0, 1.0)
    p = fails / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def measure_failure_rate(pa, pb, pc, k, cases, seed):
    """对同一构造输入重复 cases 次独立验证（每次独立种子流），统计误判率。"""
    src = F.SeededSource(seed)
    fails = 0
    for _ in range(cases):
        if F.verify_packed(pa, pb, pc, src, k, check=False):
            fails += 1
    return fails


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    summary = {}
    t_all = time.perf_counter()

    a, b, c = rank1_witness()
    pa, pb, pc = (F.BitMatrix.from_rows(m) for m in (a, b, c))

    # ------------------------------------------------------------------ #
    print("=" * 78)
    print("实验 1  确定输入（AB == C）：假阳性率必须为 0")
    print("=" * 78)
    ok_cases, ok_k = 100_000, 20
    src = F.SeededSource(10_001)
    t0 = time.perf_counter()
    wrong_rejects = 0
    for _ in range(ok_cases):
        if not F.verify_packed(pa, pb, pa, src, ok_k, check=False):  # C=AB
            wrong_rejects += 1
    dt = time.perf_counter() - t0
    print(f"cases={ok_cases:,}  k={ok_k}  误拒={wrong_rejects}  "
          f"假阳性率={wrong_rejects/ok_cases:.3e}（理论 0）  耗时 {dt:.2f}s")
    summary["deterministic"] = {
        "cases": ok_cases, "k": ok_k, "false_rejects": wrong_rejects,
        "theory": 0.0, "seconds": round(dt, 3),
    }

    # ------------------------------------------------------------------ #
    print()
    print("=" * 78)
    print("实验 2  构造性最坏输入（rank-1 D）：实测失败率 vs 理论 2^-k（几何衰减）")
    print("=" * 78)
    print(f"{'k':>3} {'cases':>10} {'fails':>8} {'实测p̂':>12} {'理论2^-k':>12} "
          f"{'Wilson 95% CI':>26} {'比值 p̂/2^-k':>12}")
    decay = []
    for k in range(1, 13):
        cases = 30_000
        fails = measure_failure_rate(pa, pb, pc, k, cases, seed=50_000 + k)
        p_hat = fails / cases
        lo, hi = wilson_ci(fails, cases)
        theo = 2.0 ** -k
        print(f"{k:>3} {cases:>10,} {fails:>8,} {p_hat:>12.6f} {theo:>12.6f} "
              f"[{lo:.6f}, {hi:.6f}] {p_hat/theo:>12.3f}")
        decay.append({"k": k, "cases": cases, "fails": fails,
                      "p_hat": p_hat, "theory": theo, "ci": [lo, hi]})
    summary["geometric_decay"] = decay

    # rank-2 D：单次误判率应 ≈ 1/4 < 1/2，说明 1/2 是一致上界、rank-1 取等
    c2 = [row[:] for row in eye(N)]
    c2[0][0] ^= 1
    c2[1][1] ^= 1
    pc2 = F.BitMatrix.from_rows(c2)
    trials = 200_000
    src = F.SeededSource(77_777)
    acc = sum(F.freivalds_once_packed(pa, pb, pc2, src) for _ in range(trials))
    lo, hi = wilson_ci(acc, trials)
    print(f"\n对照 rank-2 D：单次接受率 {acc/trials:.4f}，CI [{lo:.4f}, {hi:.4f}]"
          f"（理论 1/4，< 1/2 上界）")
    summary["rank2_single_trial"] = {"trials": trials, "accepts": acc,
                                     "p_hat": acc / trials, "ci": [lo, hi]}

    # ------------------------------------------------------------------ #
    print()
    print("=" * 78)
    print("实验 3  严格目标错误率：ε=1e-6 (k=20) 与 ε=1e-12 (k=40)")
    print("=" * 78)
    strict = []
    for eps, cases in ((1e-6, 2_000_000), (1e-12, 200_000)):
        k = F.trials_needed(eps)
        t0 = time.perf_counter()
        fails = measure_failure_rate(pa, pb, pc, k, cases, seed=90_000 + k)
        dt = time.perf_counter() - t0
        lo, hi = wilson_ci(fails, cases)
        bound = 2.0 ** -k
        print(f"ε={eps:.0e}  k={k}  理论失败率≤{bound:.3e}≤ε  "
              f"cases={cases:,}  实测失败={fails}  p̂={fails/cases:.3e}  "
              f"CI 上界={hi:.3e}  耗时 {dt:.2f}s")
        strict.append({"epsilon": eps, "k": k, "bound": bound, "cases": cases,
                       "fails": fails, "ci": [lo, hi], "seconds": round(dt, 3)})
    print("注：ε=1e-12 的直接测量需 ~1e13 次试验（不可行），故用实验 2 的")
    print("    几何衰减外推 + 试验独立性保证；这正是闭式界 k=ceil(-log2 ε) 的意义。")
    summary["strict_targets"] = strict

    # ------------------------------------------------------------------ #
    print()
    print("=" * 78)
    print("实验 4  随机源退化：健康检查必须检出；不检查则保证失效（演示）")
    print("=" * 78)
    deg = {}
    for name, mk in (("ConstantBitSource(0)", lambda: F.ConstantBitSource(0)),
                     ("ConstantBitSource(1)", lambda: F.ConstantBitSource(1)),
                     ("FixedVectorSource([1,0,1,1,0,1,0,1])",
                      lambda: F.FixedVectorSource([1, 0, 1, 1, 0, 1, 0, 1]))):
        try:
            F.verify_packed(pa, pb, pc, mk(), k=10)
            deg[name] = "未检出（BUG）"
        except F.DegenerateRandomnessError as e:
            deg[name] = f"已检出: {e}"
        print(f"  {name:45s} -> {deg[name]}")
    # 不检查：固定一个 Dr=0 的向量，k=100 仍全部 ACCEPT（保证彻底失效）
    r = [0, 1, 0, 0, 0, 0, 0, 0]
    fooled = F.verify_packed(pa, pb, pc, F.FixedVectorSource(r), k=100, check=False)
    print(f"  去掉健康检查后 FixedVectorSource(Dr=0) k=100 仍误判 ACCEPT: {fooled}")
    deg["without_check_k100_fooled"] = fooled
    summary["degenerate"] = deg

    # ------------------------------------------------------------------ #
    print()
    print("=" * 78)
    print("实验 5  可复现性：同种子两次运行，逐条对拍（写 artifacts/repro_run*.csv）")
    print("=" * 78)
    repro_cases = 20_000
    paths = []
    for run in (1, 2):
        path = os.path.join(ART, f"repro_run{run}.csv")
        src = F.SeededSource(424242)
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["case", "seed", "k", "verdict"])
            for i in range(repro_cases):
                v = F.verify_packed(pa, pb, pc, src, 10, check=False)
                w.writerow([i, 424242, 10, int(v)])
        paths.append(path)
    h1, h2 = (sha256_file(p) for p in paths)
    identical = h1 == h2
    print(f"  run1 sha256={h1[:16]}…  run2 sha256={h2[:16]}…  完全一致: {identical}")
    summary["reproducibility"] = {"cases": repro_cases, "sha256_run1": h1,
                                  "sha256_run2": h2, "identical": identical}

    # ------------------------------------------------------------------ #
    print()
    print("=" * 78)
    print("实验 6  对拍：朴素实现 vs 位打包实现（写 artifacts/differential.csv）")
    print("=" * 78)
    rng = F.SeededSource(31337)
    mismatches = 0
    diff_path = os.path.join(ART, "differential.csv")
    with open(diff_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case", "n", "seed", "k", "corrupt", "verdict_naive",
                    "verdict_packed"])
        for case in range(2000):
            n = 1 + case % 12
            am = [[rng.random_bit() for _ in range(n)] for _ in range(n)]
            bm = [[rng.random_bit() for _ in range(n)] for _ in range(n)]
            cm = F.gf2_matmul(am, bm)
            corrupt = case % 3 == 0
            if corrupt:
                cm[case % n][(case * 7) % n] ^= 1
            qa, qb, qc = (F.BitMatrix.from_rows(m) for m in (am, bm, cm))
            seed, k = 600_000 + case, 1 + case % 6
            vn = F.verify_naive(am, bm, cm, F.SeededSource(seed), k)
            vp = F.verify_packed(qa, qb, qc, F.SeededSource(seed), k)
            mismatches += vn != vp
            w.writerow([case, n, seed, k, int(corrupt), int(vn), int(vp)])
    print(f"  2000 个随机用例（1/3 注入错误），不一致数 = {mismatches}")
    print(f"  文件 sha256 = {sha256_file(diff_path)[:16]}…")
    summary["differential"] = {"cases": 2000, "mismatches": mismatches,
                               "sha256": sha256_file(diff_path)}

    # ------------------------------------------------------------------ #
    print()
    print("=" * 78)
    print("实验 7  性能：1,000,000 次重复的耗时与内存峰值")
    print("=" * 78)
    # (a) 100 万次单次试验：先裸跑计时（tracemalloc 会拖慢），再单独测内存
    t0 = time.perf_counter()
    src = F.SeededSource(1_000_000)
    acc = 0
    for _ in range(1_000_000):
        acc += F.freivalds_once_packed(pa, pb, pc, src)
    dt = time.perf_counter() - t0
    tracemalloc.start()
    src = F.SeededSource(1_000_000)
    acc2 = 0
    for _ in range(1_000_000):
        acc2 += F.freivalds_once_packed(pa, pb, pc, src)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert acc == acc2  # 同种子两次运行结果一致
    print(f"  (a) 1,000,000 次单次试验：{dt:.3f}s，{dt/1e6*1e6:.2f} µs/次，"
          f"tracemalloc 峰值 {peak/1024:.1f} KiB（流式，无累积）")
    perf = {"single_trials": {"count": 1_000_000, "seconds": round(dt, 3),
                              "us_per_trial": round(dt, 3),
                              "tracemalloc_peak_kib": round(peak / 1024, 1)}}
    # (b) 25,000 次完整验证 × k=40 = 100 万次重复（含每验证一次的向量生成）
    t0 = time.perf_counter()
    src = F.SeededSource(2_000_000)
    accepts = 0
    for _ in range(25_000):
        accepts += F.verify_packed(pa, pb, pc, src, 40, check=False)
    dt2 = time.perf_counter() - t0
    print(f"  (b) 25,000 次 verify(k=40)（短路，实际 {accepts} 次误判）：{dt2:.3f}s")
    perf["verify_k40_x25000"] = {"seconds": round(dt2, 3), "false_accepts": accepts}
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f"  进程峰值 RSS（整体）：{rss/1024:.1f} MiB")
    perf["process_peak_rss_mib"] = round(rss / 1024, 1)
    summary["performance"] = perf

    summary["total_seconds"] = round(time.perf_counter() - t_all, 2)
    with open(os.path.join(ART, "experiment_summary.json"), "w") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print()
    print(f"全部实验完成，总耗时 {summary['total_seconds']}s；"
          f"汇总写入 artifacts/experiment_summary.json")


if __name__ == "__main__":
    main()

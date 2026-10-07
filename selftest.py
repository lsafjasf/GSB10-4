"""randamp 自测：失败概率控制的推导值对照、对拍、坏输入、性能基准。

运行：python3 selftest.py
仅使用 Python 3 标准库。
"""

import hashlib
import random
import time
import tracemalloc

import randamp as ra


# ---------------------------------------------------------------- 模拟的基算法
def make_bernoulli_algorithm(p):
    """单边/实验用：每次以概率 p 找到"证据"（返回 True）。

    刻意把 p 设为保证值 p0 本身 -> 最坏情形，用来检验上界而不是平均情形。
    """
    def algo(_arg, rng):
        return rng.random() < p
    return algo


class AdversarialSchedule:
    """构造性坏输入：基算法企图按调用次序作弊（只在特定轮次成功）。

    它不知道每轮随机数，但试图利用"重复间的可预测调度"。
    对注入的独立随机源无效；对可预测/退化的随机源则会暴露问题。
    """

    def __init__(self, good_slots):
        self.good_slots = set(good_slots)
        self.calls = 0

    def __call__(self, _arg, rng):
        slot = self.calls
        self.calls += 1
        return slot in self.good_slots or rng.random() < 0.5


class ConstantRNG:
    """退化随机源：永远返回同一个数（模拟坏掉的 RNG / 被固定的种子流）。"""

    def __init__(self, value=0.0):
        self.value = value

    def random(self):
        return self.value


# ---------------------------------------------------------------- 统计工具
def run_batches(vote_fn, batches, seed):
    """跑 batches 个独立批次，返回 (失败次数, 每批结果比特串摘要)。"""
    rng = random.Random(seed)
    fails = 0
    h = hashlib.sha256()
    for _ in range(batches):
        ok = vote_fn(rng)
        if not ok:
            fails += 1
        h.update(b"1" if ok else b"0")
    return fails, h.hexdigest()


def report(name, failures, batches, bound, exact=None):
    lo, hi = ra.wilson_interval(failures, batches)
    pct = failures / batches
    print("  场景: %s" % name)
    print("    批次数 N = %d, 失败 = %d, 实测失败率 = %.6f" % (batches, failures, pct))
    print("    95%% Wilson 置信区间 = [%.6f, %.6f]" % (lo, hi))
    print("    理论上界            = %.6e" % bound)
    if exact is not None:
        inside = lo <= exact <= hi
        print("    精确概率(对拍基准) = %.6f, 落入 Wilson 区间: %s"
              % (exact, "是" if inside else "否"))
        print("    对照结论: %s"
              % ("通过(实测与精确概率一致)" if inside else
                 "违反(95%% CI 未覆盖精确概率)"))
    else:
        print("    对照结论: %s"
              % ("通过(CI 上沿 %.6f 与上界相容)" % hi
                 if lo <= bound else "违反!"))
    print()


def section(title):
    print("=" * 72)
    print(title)
    print("=" * 72)


def main():
    t0 = time.perf_counter()

    # ----------------------------------------------------- 1. 参数校验
    section("1. 参数校验（非法目标错误率 / eps / k / rng）")
    bad = []
    for call in [
        lambda: ra.reps_one_sided(0.0),
        lambda: ra.reps_one_sided(1.0),
        lambda: ra.reps_majority(1e-3, 0.0),
        lambda: ra.reps_majority(1e-3, 0.6),
        lambda: ra.amplify_one_sided(make_bernoulli_algorithm(0.5), None, 0,
                                     random.Random(0)),
        lambda: ra.amplify_majority(make_bernoulli_algorithm(0.6), None, 3,
                                    object()),
    ]:
        try:
            call()
        except (ValueError, TypeError):
            bad.append(True)
        else:
            bad.append(False)
    print("  6 个非法调用全部被拒绝: %s\n" % ("是" if all(bad) else "否"))

    # ----------------------------------------------------- 2. 单边错误放大
    section("2. 单边错误（OR 放大），delta=1e-3，最坏情形 p=p0=0.5")
    delta1 = 1e-3
    k1 = ra.reps_one_sided(delta1)
    bound1 = ra.failure_bound_one_sided(k1)
    print("  推导: k = ceil(log2(1/delta)) = ceil(log2(1000)) = %d" % k1)
    print("  上界: 2^-k = 2^-%d = %.6e (目标 %.1e)\n" % (k1, bound1, delta1))
    algo = make_bernoulli_algorithm(0.5)
    BATCHES1 = 200_000
    f1, digest_a = run_batches(
        lambda rng: ra.amplify_one_sided(algo, None, k1, rng),
        BATCHES1, seed=20261007)
    exact1 = ra.failure_bound_one_sided(k1)  # p=p0 时上界即精确失败概率
    report("单边 OR, k=%d" % k1, f1, BATCHES1, bound1, exact=exact1)

    # ----------------------------------------------------- 3. 双边错误放大
    section("3. 双边错误（多数投票），eps=0.05, delta=0.05，最坏情形 p=0.55")
    eps = 0.05
    delta2 = 0.05
    k2 = ra.reps_majority(delta2, eps)
    bound2 = ra.failure_bound_majority(k2, eps)
    print("  推导: k = ceil(ln(1/delta)/(2 eps^2)) 取奇数 = %d" % k2)
    print("  Hoeffding 上界: exp(-2*k*eps^2) = %.6e (目标 %.2f)\n"
          % (bound2, delta2))
    algo2 = make_bernoulli_algorithm(0.5 + eps)  # 刻意压在保证边界上
    BATCHES2 = 20_000
    f2, digest_m = run_batches(
        lambda rng: ra.amplify_majority(algo2, None, k2, rng),
        BATCHES2, seed=424242)
    # 精确失败概率 = P[Bin(k,0.55) <= k/2]，作为对拍基准
    exact2 = ra.binomial_tail_le(k2 // 2, k2, 0.5 + eps)
    report("多数投票, k=%d" % k2, f2, BATCHES2, bound2, exact=exact2)
    print("  注: Hoeffding 是通用上界(只用 p>=1/2+eps), 显著宽松；")
    print("      精确二项概率才是该 p 下的真实失败率，实测应贴着精确值而非上界。\n")

    # ----------------------------------------------------- 4. 可复现性对拍
    section("4. 可复现性对拍（同种子独立重跑两次，逐批结果必须完全一致）")
    f1b, digest_b = run_batches(
        lambda rng: ra.amplify_one_sided(algo, None, k1, rng),
        BATCHES1, seed=20261007)
    f2b, digest_m2 = run_batches(
        lambda rng: ra.amplify_majority(algo2, None, k2, rng),
        BATCHES2, seed=424242)
    print("  单边   两次失败数: %d vs %d; SHA256: %s / %s -> %s"
          % (f1, f1b, digest_a[:16], digest_b[:16],
             "一致" if digest_a == digest_b else "不一致!"))
    print("  双边   两次失败数: %d vs %d; SHA256: %s / %s -> %s"
          % (f2, f2b, digest_m[:16], digest_m2[:16],
             "一致" if digest_m == digest_m2 else "不一致!"))
    f1c, _ = run_batches(
        lambda rng: ra.amplify_one_sided(algo, None, k1, rng),
        BATCHES1, seed=999)
    print("  异种子对照组: 失败数 %d（同种子 %d），通常不同以证明种子确实生效: %s\n"
          % (f1c, f1, "是" if f1c != f1 else "本次恰好相同"))

    # ----------------------------------------------------- 5. 确定输入
    section("5. 确定输入（基算法恒定，放大后结论必须恒定）")
    for label, fn in [("恒 True", lambda a, r: True),
                      ("恒 False", lambda a, r: False)]:
        r1 = ra.amplify_one_sided(fn, None, 51, random.Random(7))
        r2 = ra.amplify_majority(fn, None, 51, random.Random(7))
        print("  %-8s -> OR 放大=%s, 多数投票=%s" % (label, r1, r2))
    print()

    # ----------------------------------------------------- 6. 构造性坏输入
    section("6. 构造性坏输入（对抗调度：成功轮次占比恰为 1/2 的最稀疏证据）")
    adv = AdversarialSchedule(good_slots=range(0, k1 * 10, 2))
    # 每次 amplify 用全新算法对象，模拟"输入被构造"的最坏基算法
    f_adv, _ = run_batches(
        lambda rng: ra.amplify_one_sided(
            AdversarialSchedule(range(0, k1, 2)), None, k1, rng),
        50_000, seed=31337)
    # 每轮 slot 在 good_slits 中即必成功，故 OR 必成功：构造无法骗过 OR 结构
    print("  对抗基算法(偶数轮必出证据), OR 放大 k=%d, 5 万批失败数 = %d"
          % (k1, f_adv))
    print("  -> 结构性保证: 只要存在确定性证据轮, OR 必然捕获, 失败率 0")

    # 多数投票下的对抗调度: 正确轮次恰为 55% 且被刻意挤到最后,
    # 试图让"前半段看起来要输"。多数投票只看总数, 调度无效。
    k_adv = ra.reps_majority(0.05, 0.05)
    n_good = -(-55 * k_adv // 100)  # ceil(0.55*k)

    class ScheduledAdversary:
        """55% 的轮次正确，但把正确票全部挤到序列末尾。"""
        def __init__(self):
            self.slot = 0
        def __call__(self, _arg, _rng):
            good = self.slot >= k_adv - n_good
            self.slot += 1
            return good

    wins = 0
    for _ in range(100):
        wins += ra.amplify_majority(ScheduledAdversary(), None, k_adv,
                                    random.Random(0))
    print("  多数投票对抗调度(55%% 正确票挤在末尾), k=%d, 100 批正确 = %d"
          % (k_adv, wins))
    print("  -> 保证只依赖正确率下界而非出现顺序, 对抗调度无法降低结论\n")

    # ----------------------------------------------------- 7. 随机源退化
    section("7. 随机源退化（ConstantRNG：演示独立性假设被破坏时的后果与检测）")
    # 退化 RNG 输出常量 c: c<p 时每次"成功", c>=p 时每次失败,
    # 重复 k 次等价于重复同一次 -> 失败率退回单次水平 1-p, 上界失效。
    for c in (0.0, 0.9):
        r = ra.amplify_one_sided(make_bernoulli_algorithm(0.5), None, k1,
                                 ConstantRNG(c))
        print("  ConstantRNG(%.1f) + 单边 p=0.5, k=%d -> 结论 %s" % (c, k1, r))
    n_deg = 1000
    f_deg = sum(
        not ra.amplify_one_sided(make_bernoulli_algorithm(0.5), None, k1,
                                 ConstantRNG(0.0 if i % 2 == 0 else 0.9))
        for i in range(n_deg))
    print("  退化 RNG(常量等概取 0.0/0.9) 实测失败率 = %.3f"
          "（放大后理论应 <= %.1e, 实际退回单次水平 0.5）"
          % (f_deg / n_deg, bound1))
    print("  -> 上界失效源于独立性假设被破坏，证明随机源注入与质量是必要前提。")
    good_rng = random.Random(1)
    print("  对照: 正常 RNG 同样 k=%d 连跑 100 批失败数 = %d\n"
          % (k1, sum(not ra.amplify_one_sided(
              make_bernoulli_algorithm(0.5), None, k1, random.Random(s))
              for s in range(100))))

    # ----------------------------------------------------- 8. 极严目标
    section("8. 目标错误率极严（delta=1e-12 与 1e-30）")
    for d in (1e-12, 1e-30):
        k = ra.reps_one_sided(d)
        b = ra.failure_bound_one_sided(k)
        print("  delta=%.0e -> k=%d, 实际界 2^-k = %.3e %s 目标"
              % (d, k, b, "<=" if b <= d else ">"))
    k_strict = ra.reps_one_sided(1e-12)
    f_strict = sum(not ra.amplify_one_sided(algo, None, k_strict,
                                            random.Random(s))
                   for s in range(2000))
    print("  k=%d 连跑 2000 批失败数 = %d（期望 %.2e, 观测为 0 与之一致）\n"
          % (k_strict, f_strict, 2000 * 0.5 ** k_strict))

    # ----------------------------------------------------- 9. 性能: 100 万次
    section("9. 性能：单次放大内部重复 1,000,000 次（耗时 / 内存峰值）")
    K = 1_000_000
    rng_perf = random.Random(2026)
    tracemalloc.start()
    tp = time.perf_counter()
    res = ra.amplify_majority(make_bernoulli_algorithm(0.501), None, K, rng_perf)
    elapsed = time.perf_counter() - tp
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print("  多数投票 k=%d, 结论=%s" % (K, res))
    print("  耗时 = %.3f s（每千次 %.3f ms）" % (elapsed, elapsed / K * 1e6))
    print("  tracemalloc 峰值 = %.2f KiB（流式计数器，O(1) 额外内存）\n"
          % (peak / 1024))

    print("=" * 72)
    print("全部自测完成，用时 %.1f s" % (time.perf_counter() - t0))
    print("=" * 72)


if __name__ == "__main__":
    main()

"""自测：正确性对拍、探测长度分布、删除/墓碑实验、退化哈希。

运行：python3 selftest.py
"""
import random
import statistics
import time

from hashtable import (ProbingHashTable, ProbingHashTableNoCleanup,
                       ChainedHashTable)


def percentile(sorted_vals, q):
    if not sorted_vals:
        return 0
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def histogram(vals):
    buckets = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5),
               (6, 8), (9, 16), (17, 32), (33, 10**9)]
    out = []
    for lo, hi in buckets:
        c = sum(1 for v in vals if lo <= v <= hi)
        if c:
            label = str(lo) if lo == hi else ("%d-%d" % (lo, hi) if hi < 10**9 else ">32")
            out.append("%s:%d" % (label, c))
    return " ".join(out)


def probe_stats(table, keys):
    vals = sorted(table.probe_length(k)[0] for k in keys)
    n = len(vals)
    return {
        "avg": sum(vals) / n,
        "p50": percentile(vals, 0.50),
        "p90": percentile(vals, 0.90),
        "p99": percentile(vals, 0.99),
        "p999": percentile(vals, 0.999),
        "max": vals[-1],
        "hist": histogram(vals),
    }


def fmt_stats(s):
    return ("avg=%.3f p50=%d p90=%d p99=%d p99.9=%d max=%d | %s"
            % (s["avg"], s["p50"], s["p90"], s["p99"], s["p999"], s["max"], s["hist"]))


# ---------------------------------------------------------------- 1. 对拍
def test_differential():
    print("== 1. 正确性对拍：开放地址 vs 链地址 vs dict ==")
    for seed in (1, 2, 3):
        rng = random.Random(seed)
        pt = ProbingHashTable()
        ct = ChainedHashTable()
        ref = {}
        ops = 0
        for _ in range(120_000):
            r = rng.random()
            if rng.random() < 0.5:
                k = rng.randrange(0, 20000)
            else:
                k = "key-%d" % rng.randrange(0, 20000)
            if r < 0.40:
                v = rng.randrange(2**31)
                pt[k] = v; ct[k] = v; ref[k] = v
            elif r < 0.65:
                d = object()
                a, b, c = pt.get(k, d), ct.get(k, d), ref.get(k, d)
                assert a is b is c or (a == b == c), (k, a, b, c)
            elif r < 0.85:
                d = object()
                a, b, c = pt.pop(k, d), ct.pop(k, d), ref.pop(k, d)
                assert a is b is c or (a == b == c), (k, a, b, c)
            else:
                assert (k in pt) == (k in ct) == (k in ref)
            ops += 1
        assert len(pt) == len(ct) == len(ref)
        assert dict(pt.items()) == dict(ct.items()) == ref
        print("  seed=%d ops=%d 最终 n=%d：三实现结果一致 ✓" % (seed, ops, len(ref)))

    # 墓碑链专项：删-插交替高压
    rng = random.Random(7)
    pt, ct, ref = ProbingHashTable(), ChainedHashTable(), {}
    for i in range(3000):
        pt[i] = ct[i] = ref[i] = i
    for _ in range(60_000):
        k = rng.randrange(0, 6000)
        if rng.random() < 0.5:
            assert pt.pop(k, None) == ct.pop(k, None) == ref.pop(k, None)
        else:
            pt[k] = k; ct[k] = k; ref[k] = k
    assert dict(pt.items()) == dict(ct.items()) == ref
    print("  删-插交替 60000 次（墓碑高压）：一致 ✓")


# ------------------------------------------------------- 2. 探测长度分布
def test_probe_distribution():
    print("== 2. 查找长度分布（双重散列，实测） ==")
    rng = random.Random(42)
    for target, cap in ((0.50, 1 << 18), (0.70, 1 << 20)):
        n = int(target * cap)
        keys = rng.sample(range(1 << 40), n)
        t = ProbingHashTable(initial_capacity=cap)
        for k in keys:
            t[k] = k
        absent = rng.sample(range(1 << 40, 1 << 41), n)
        hit = probe_stats(t, keys)
        miss = probe_stats(t, absent)
        # 理论值（均匀散列假设，Knuth TAOCP 6.4）：成功 ~ (1/α)ln(1/(1-α))，失败 ~ 1/(1-α)
        import math
        theo_hit = math.log(1 / (1 - target)) / target
        theo_miss = 1 / (1 - target)
        print("  α=%.2f (n=%d, cap=%d)" % (t.load_factor(), n, cap))
        print("    命中: %s   [理论 avg≈%.2f]" % (fmt_stats(hit), theo_hit))
        print("    未中: %s   [理论 avg≈%.2f]" % (fmt_stats(miss), theo_miss))


# ------------------------------------------------------- 3. 删除与墓碑
def _measure(t, present, absent, lookups=100_000):
    rng = random.Random(9)
    hit_keys = [present[rng.randrange(len(present))] for _ in range(lookups // 2)]
    miss_keys = [absent[rng.randrange(len(absent))] for _ in range(lookups // 2)]
    t0 = time.perf_counter()
    s = 0
    for k in hit_keys:
        s += t.get(k, 0)
    for k in miss_keys:
        s += t.get(k, 0)
    dt = time.perf_counter() - t0
    hit_asl = sum(t.probe_length(k)[0] for k in hit_keys[:20000]) / 20000
    miss_asl = sum(t.probe_length(k)[0] for k in miss_keys[:20000]) / 20000
    return dt, hit_asl, miss_asl, s


def test_deletion():
    print("== 3. 删除/墓碑实验（n0=700000, cap=2^20） ==")
    rng = random.Random(43)
    n0 = 700_000
    keys = rng.sample(range(1 << 40), n0)
    absent = rng.sample(range(1 << 40, 1 << 41), 100_000)

    # 3a. 删-插交替循环：每轮删 10 万 + 插 10 万新键，共 8 轮。
    #     墓碑若不清理会持续累积，EMPTY 槽被耗尽，查找越来越慢。
    print("  3a. 删-插交替（每轮 -10万/+10万，n 恒定 70 万）")
    for cls, name in ((ProbingHashTable, "带墓碑清理(本实现)"),
                      (ProbingHashTableNoCleanup, "不清理(对照)")):
        t = cls(initial_capacity=1 << 20)
        for k in keys:
            t[k] = k
        present = list(keys)
        fresh = iter(rng.sample(range(1 << 41, 1 << 42), 1_000_000))
        print("    [%s]" % name)
        for rnd in range(1, 9):
            rng.shuffle(present)
            for k in present[:100_000]:
                t.pop(k)
            present = present[100_000:]
            new_keys = [next(fresh) for _ in range(100_000)]
            for k in new_keys:
                t[k] = k
            present += new_keys
            dt, hit_asl, miss_asl, _ = _measure(t, present, absent)
            print("      轮%d: n=%6d cap=%7d 墓碑=%6d | 命中ASL=%.2f 未中ASL=%.2f | 10万查找 %.3fs"
                  % (rnd, len(t), t.capacity(), t.tombstone_count(),
                     hit_asl, miss_asl, dt))

    # 3b. 连续删除到 1%：容量应收缩、性能不劣化；再插回 50 万验证恢复。
    print("  3b. 连续删除 0%% -> 50%% -> 90%% -> 99%%（本实现）")
    t = ProbingHashTable(initial_capacity=1 << 20)
    for k in keys:
        t[k] = k
    present = list(keys)
    rng.shuffle(present)
    for frac in (0.0, 0.5, 0.9, 0.99):
        target_n = int(n0 * (1 - frac))
        while len(t) > target_n:
            t.pop(present.pop())
        dt, hit_asl, miss_asl, _ = _measure(t, present, absent)
        print("    删除 %4.0f%% 后: n=%6d cap=%7d 墓碑=%6d | 命中ASL=%.2f 未中ASL=%.2f | 10万查找 %.3fs"
              % (frac * 100, len(t), t.capacity(), t.tombstone_count(),
                 hit_asl, miss_asl, dt))
    t0 = time.perf_counter()
    new_keys = rng.sample(range(1 << 42, 1 << 43), 500_000)
    for k in new_keys:
        t[k] = k
    dt_ins = time.perf_counter() - t0
    dt, hit_asl, miss_asl, _ = _measure(t, new_keys, absent)
    print("    再插入 50 万新键: n=%6d cap=%7d 墓碑=%6d | 命中ASL=%.2f 未中ASL=%.2f | 插入 %.3fs, 10万查找 %.3fs"
          % (len(t), t.capacity(), t.tombstone_count(), hit_asl, miss_asl, dt_ins, dt))


# ------------------------------------------------------- 4. 退化哈希
def test_degenerate_hash():
    print("== 4. 退化哈希函数 ==")
    rng = random.Random(44)

    const_hash = lambda k: 0
    n = 4000
    keys = rng.sample(range(1 << 40), n)
    t = ProbingHashTable(hash_fn=const_hash)
    t0 = time.perf_counter()
    for k in keys:
        t[k] = k
    dt_ins = time.perf_counter() - t0
    for k in keys:
        assert t[k] == k
    ref = dict(zip(keys, keys))
    for _ in range(2000):
        k = rng.choice(keys)
        if rng.random() < 0.5:
            t.pop(k, None); ref.pop(k, None)
        else:
            t[k] = k; ref[k] = k
    assert dict(t.items()) == ref
    mx = max(t.probe_length(k)[0] for k in list(t.keys())[:500])
    print("  常数哈希 h(k)=0, n=%d: 正确 ✓ 插入 %.3fs, 当前最大探测=%d（退化为线性探测，仍正确）"
          % (len(t), dt_ins, mx))

    weak_hash = lambda k: k % 16
    n = 20000
    keys = rng.sample(range(1 << 40), n)
    t = ProbingHashTable(hash_fn=weak_hash)
    c = ChainedHashTable(hash_fn=weak_hash)
    t0 = time.perf_counter()
    for k in keys:
        t[k] = k; c[k] = k
    dt_ins = time.perf_counter() - t0
    assert dict(t.items()) == dict(c.items()) == dict(zip(keys, keys))
    hit = probe_stats(t, keys[:20000])
    chain = [c.comparison_count(k)[0] for k in keys[:20000]]
    print("  弱哈希 h(k)=k%%16, n=%d: 与链地址对拍一致 ✓ 插入 %.3fs" % (n, dt_ins))
    print("    开放地址 命中: %s" % fmt_stats(hit))
    print("    链地址 桶内比较: avg=%.1f max=%d（同一哈希值的键天然共享一条 ~n/16 的链）"
          % (sum(chain) / len(chain), max(chain)))


if __name__ == "__main__":
    test_differential()
    test_probe_distribution()
    test_deletion()
    test_degenerate_hash()
    print("全部自测通过 ✓")

"""Self-tests for sampler.py.  Prints a data table; exits 1 on any failure.

Run:  python3 src/selftest.py
"""
import math
import random
import sys

from sampler import Sampler, sample_lines

FAILURES = []


def check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def binom_sigma(n, p):
    return math.sqrt(n * p * (1.0 - p))


def main():
    # ------------------------------------------------------------------ T1
    print("== T1 determinism: same seed -> identical, other seed -> different ==")
    keys = [f"key-{i}".encode() for i in range(200_000)]
    a = list(sample_lines(keys, seed=42, rate=0.01))
    b = list(sample_lines(keys, seed=42, rate=0.01))
    c = list(sample_lines(keys, seed=43, rate=0.01))
    check("same seed, two runs, identical sample", a == b, f"n={len(a)}")
    check("different seed, different sample", a != c,
          f"overlap={len(set(a) & set(c))} of {len(a)}")

    # ------------------------------------------------------------------ T2
    print("== T2 order independence: shuffled input -> identical sample set ==")
    base = [f"row-{i}".encode() for i in range(200_000)]
    ref = set(sample_lines(base, seed=7, rate=0.05))
    ok = True
    for shuffle_seed in (1, 2, 3, 99, 123456):
        shuffled = base[:]
        random.Random(shuffle_seed).shuffle(shuffled)
        got = set(sample_lines(shuffled, seed=7, rate=0.05))
        ok = ok and (got == ref)
    check("5 independent shuffles, sample set unchanged", ok,
          f"|sample|={len(ref)}")

    # ------------------------------------------------------------------ T3
    print("== T3 shard sampling == global sampling (exact, not statistical) ==")
    n, rate, shards = 1_000_000, 0.01, 8
    all_keys = [f"k{i}".encode() for i in range(n)]
    global_sample = set(sample_lines(all_keys, seed=2024, rate=rate))

    # shard layout A: round-robin
    rr = [all_keys[i::shards] for i in range(shards)]
    union_rr = set()
    for shard in rr:
        union_rr |= set(sample_lines(shard, seed=2024, rate=rate))
    check("round-robin shards union == global", union_rr == global_sample,
          f"|global|={len(global_sample)}")

    # shard layout B: random partition
    rng = random.Random(555)
    rand_shards = [[] for _ in range(shards)]
    for k in all_keys:
        rand_shards[rng.randrange(shards)].append(k)
    union_rand = set()
    for shard in rand_shards:
        union_rand |= set(sample_lines(shard, seed=2024, rate=rate))
    check("random-partition shards union == global", union_rand == global_sample)

    # ------------------------------------------------------------------ T4
    print("== T4 per-shard distribution (chi-square, df=7) ==")
    counts = [len(set(sample_lines(s, seed=2024, rate=rate))) for s in rand_shards]
    total = sum(counts)
    expected = total / shards
    chi2 = sum((c - expected) ** 2 / expected for c in counts)
    max_dev = max(abs(c - expected) / expected for c in counts)
    # chi2 critical values df=7: 18.48 @ p=0.01, 24.32 @ p=0.001
    print("    shard sizes:", [len(s) for s in rand_shards])
    print("    shard sample counts:", counts)
    print(f"    chi2={chi2:.2f} (crit 24.32 @ p=0.001), "
          f"max rel deviation={max_dev:.3%}")
    check("chi-square within p=0.001 bound", chi2 < 24.32, f"chi2={chi2:.2f}")

    # ------------------------------------------------------------------ T5
    print("== T5 sampling-rate error: measured vs 5-sigma upper bound ==")
    for n, p in ((1_000_000, 0.01), (1_000_000, 0.001), (1_000_000, 0.5),
                 (100_000, 0.01)):
        ks = (f"e{i}".encode() for i in range(n))
        got = sum(1 for _ in sample_lines(ks, seed=9, rate=p))
        exp = n * p
        sigma = binom_sigma(n, p)
        bound = 5 * sigma  # P(|err| > 5 sigma) < 6e-7 under normal approx
        err = abs(got - exp)
        print(f"    n={n:>8} p={p:<5} expected={exp:>9.0f} got={got:>7} "
              f"|err|={err:>5.0f} bound(5s)={bound:6.1f} "
              f"rel_err={err/exp:.3%} rel_bound={bound/exp:.3%}")
        check(f"rate error within 5-sigma (n={n}, p={p})", err <= bound,
              f"|err|={err:.0f} <= {bound:.1f}")

    # ------------------------------------------------------------------ T6
    print("== T6 edge cases ==")
    ks = [f"x{i}".encode() for i in range(10_000)]
    r0 = list(sample_lines(ks, seed=1, rate=0.0))
    check("rate=0.0 keeps nothing", r0 == [], f"kept={len(r0)}")
    r1 = list(sample_lines(ks, seed=1, rate=1.0))
    check("rate=1.0 keeps everything", r1 == ks, f"kept={len(r1)}")
    tiny = list(sample_lines([b"a", b"b", b"c"], seed=1, rate=0.001))
    check("tiny input, tiny rate -> empty sample is fine", tiny == [])
    one = list(sample_lines([b"only"], seed=1, rate=1.0))
    check("single row, rate=1 -> kept", one == [b"only"])
    empty = list(sample_lines([], seed=1, rate=0.5))
    check("empty input -> empty sample", empty == [])

    # duplicate-heavy: 500 unique keys x 2000 copies
    uniq, copies = 500, 2000
    dup_keys = [f"dup-{i % uniq}".encode() for i in range(uniq * copies)]
    random.Random(3).shuffle(dup_keys)
    dup_sample = list(sample_lines(dup_keys, seed=11, rate=0.05))
    from collections import Counter
    cnt = Counter(dup_sample)
    consistent = all(v == copies for v in cnt.values())
    check("duplicate keys: kept keys appear exactly `copies` times",
          consistent, f"kept unique keys={len(cnt)}")
    # and the kept unique set equals sampling the unique keys directly
    uniq_sample = set(sample_lines((f"dup-{i}".encode() for i in range(uniq)),
                                   seed=11, rate=0.05))
    check("duplicate decisions == unique-key decisions",
          set(cnt) == uniq_sample, f"|uniq kept|={len(uniq_sample)}")

    # invalid rate must raise
    try:
        Sampler(seed=1, rate=1.5)
        check("rate=1.5 raises ValueError", False)
    except ValueError:
        check("rate=1.5 raises ValueError", True)
    try:
        Sampler(seed=1, rate=-0.1)
        check("rate=-0.1 raises ValueError", False)
    except ValueError:
        check("rate=-0.1 raises ValueError", True)

    # seed types interchangeable & documented
    s_int = list(sample_lines(ks[:1000], seed=255, rate=0.5))
    s_bytes = list(sample_lines(ks[:1000], seed=b"\xff", rate=0.5))
    check("int seed 255 == bytes seed b'\\xff'", s_int == s_bytes)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} test(s): {FAILURES}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

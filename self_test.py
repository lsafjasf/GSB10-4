"""Self-tests for sliding_median: differential testing against a naive
sort-based implementation, plus targeted edge cases.

Run: python3 self_test.py
Exits non-zero on the first failure.
"""

import random
import time
from fractions import Fraction

from sliding_median import sliding_window_medians


def naive_medians(values, k):
    """Reference: re-sort every window from scratch."""
    return [
        (lambda w: w[k // 2] if k % 2 else (Fraction(w[k // 2 - 1]) + Fraction(w[k // 2])) / 2)(
            sorted(values[i : i + k])
        )
        for i in range(len(values) - k + 1)
    ]


def check_case(name, values, k, verbose=True):
    got = list(sliding_window_medians(values, k))
    want = naive_medians(values, k)
    assert got == want, (
        f"MISMATCH in {name}: k={k}\n values={values}\n got ={got}\n want={want}"
    )
    if verbose:
        preview = ", ".join(str(m) for m in got[:8])
        suffix = ", ..." if len(got) > 8 else ""
        print(f"[ok] {name:<46} n={len(values):<7} k={k:<5} medians: {preview}{suffix}")


def main():
    print("== 1. Fixed cases (differential vs naive) ==")
    check_case("classic example", [1, 3, -1, -3, 5, 3, 6, 7], 3)
    check_case("classic example, even k", [1, 3, -1, -3, 5, 3, 6, 7], 4)
    check_case("window of one (k=1)", [9, -4, 0, 7, 7], 1)
    check_case("window equals length (k=n)", [5, 2, 8, 1], 4)
    check_case("window equals length, odd", [3, 1, 2], 3)
    check_case("all identical values", [7] * 12, 5)
    check_case("all identical values, even k", [7] * 12, 6)
    check_case("two alternating values", [1, 2] * 10, 7)
    check_case("extreme values inside window", [10**30, -(10**30), 0, 5, -7], 2)
    check_case("extreme values, odd k", [10**30, -(10**30), 0, 5, -7], 3)
    check_case("negatives and duplicates", [-5, -5, -5, 0, 0, -1], 3)

    print("\n== 2. Duplicate-heavy data (deletion stress) ==")
    rng = random.Random(20261005)
    for trial in range(300):
        n = rng.randint(1, 120)
        k = rng.randint(1, n)
        values = [rng.choice([1, 1, 1, 2, 2, 3]) for _ in range(n)]
        check_case(f"dup-heavy trial {trial}", values, k, verbose=False)
    print("[ok] 300 duplicate-heavy random trials all match naive")

    big_same = [42] * 200_000
    t0 = time.perf_counter()
    got = list(sliding_window_medians(big_same, 1000))
    dt = time.perf_counter() - t0
    assert got == [42] * (200_000 - 1000 + 1)
    print(f"[ok] all-identical n=200000 k=1000 -> every median 42 ({dt:.3f}s)")

    print("\n== 3. Randomized differential testing ==")
    distributions = {
        "small range (many dups)": lambda r: r.randint(-5, 5),
        "uniform ints": lambda r: r.randint(-10**6, 10**6),
        "huge magnitude": lambda r: r.randint(-10**18, 10**18),
        "sorted ascending": lambda r: None,  # built below
        "sorted descending": lambda r: None,
        "floats": lambda r: round(r.uniform(-100, 100), 3),
    }
    total = 0
    for name, gen in distributions.items():
        trials = 500
        for _ in range(trials):
            n = rng.randint(1, 200)
            k = rng.randint(1, n)
            if name == "sorted ascending":
                values = sorted(rng.randint(-1000, 1000) for _ in range(n))
            elif name == "sorted descending":
                values = sorted((rng.randint(-1000, 1000) for _ in range(n)), reverse=True)
            else:
                values = [gen(rng) for _ in range(n)]
            check_case(f"{name}", values, k, verbose=False)
        total += trials
        print(f"[ok] {trials} trials x {name}")
    print(f"[ok] {total} randomized trials all match naive")

    print("\n== 4. Readable differential sample (for the report) ==")
    sample_values = [rng.randint(-9, 9) for _ in range(12)]
    sample_k = 4
    got = list(sliding_window_medians(sample_values, sample_k))
    want = naive_medians(sample_values, sample_k)
    assert got == want
    print(f"values = {sample_values}, k = {sample_k}")
    for i, (g, w) in enumerate(zip(got, want)):
        window = sample_values[i : i + sample_k]
        print(f"  window[{i:>2}] {str(window):<28} sorted={sorted(window)!s:<28} "
              f"median={g}  naive={w}  match={g == w}")

    print("\n== 5. Error handling ==")
    for bad_k in (0, -3):
        try:
            list(sliding_window_medians([1, 2, 3], bad_k))
        except ValueError:
            print(f"[ok] k={bad_k} raises ValueError")
        else:
            raise AssertionError(f"k={bad_k} should raise")
    try:
        list(sliding_window_medians([1, 2], 5))
    except ValueError:
        print("[ok] k > n raises ValueError")
    else:
        raise AssertionError("k > n should raise")

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()

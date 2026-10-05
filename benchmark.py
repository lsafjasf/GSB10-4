"""Benchmark: 20 elements with half duplicates (10 distinct values x 2 copies).

Run:  python3 benchmark.py
"""

import itertools
import time
import tracemalloc

from combperm import (
    count_combinations,
    count_permutations,
    unique_combinations,
    unique_permutations,
)

ITEMS = list("aabbccddeeffgghhiijj")  # 20 elements, 10 distinct x 2 copies


def measure(fn):
    tracemalloc.start()
    t0 = time.perf_counter()
    n = fn()
    dt = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return n, dt, peak


def main():
    print("multiset: 10 distinct values x 2 copies = 20 elements "
          "(half duplicates)\n")

    # 1) Full combination enumeration, all r = 0..20 (every sub-multiset).
    def all_combos():
        return sum(1 for r in range(21) for _ in unique_combinations(ITEMS, r))

    n, dt, peak = measure(all_combos)
    formula = sum(count_combinations(ITEMS, r) for r in range(21))
    print(f"[combinations, all r] emitted={n}  formula={formula}  "
          f"match={n == formula}")
    print(f"  time={dt*1000:.1f} ms   peak mem={peak/1024:.1f} KiB\n")

    # 2) Combinations for r = 10 (the largest single layer).
    n, dt, peak = measure(lambda: sum(1 for _ in unique_combinations(ITEMS, 10)))
    print(f"[combinations, r=10] emitted={n}  formula={count_combinations(ITEMS, 10)}"
          f"  match={n == count_combinations(ITEMS, 10)}")
    print(f"  time={dt*1000:.1f} ms   peak mem={peak/1024:.1f} KiB\n")

    # 3) Permutations: total is 20! / 2**10 ~ 2.4e15 -- far too many to
    #    materialise; stream the first 1,000,000 to prove bounded memory.
    total_p = count_permutations(ITEMS)
    print(f"[permutations, full length] formula total = {total_p} "
          f"(~{total_p:.2e}); streaming first 1,000,000")
    n, dt, peak = measure(
        lambda: sum(1 for _ in itertools.islice(unique_permutations(ITEMS),
                                                1_000_000)))
    print(f"  emitted={n}  time={dt*1000:.1f} ms   "
          f"peak mem={peak/1024:.1f} KiB")
    print(f"  => memory stays O(distinct + r); the full {total_p:.2e} results"
          f" would need petabytes if materialised, but stream at constant"
          f" memory.")


if __name__ == "__main__":
    main()

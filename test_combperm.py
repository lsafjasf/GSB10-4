"""Self-tests for combperm: differential vs naive, formula counts, memory
bound, and edge cases (no duplicates / all identical / single element /
limit-terminated).  Run:  python3 test_combperm.py -v
"""

import itertools
import random
import tracemalloc
import unittest

from combperm import (
    count_combinations,
    count_permutations,
    full_permutation_count_closed_form,
    unique_combinations,
    unique_permutations,
)


def naive_combinations(items, r):
    """Enumerate with itertools, then dedup -- the naive baseline."""
    return {tuple(sorted(c)) for c in itertools.combinations(items, r)}


def naive_permutations(items, r):
    return set(itertools.permutations(items, r))


class DifferentialTest(unittest.TestCase):
    """Library output must equal enumerate-then-dedup, with no duplicates."""

    def test_random_multisets(self):
        # n <= 7 keeps full naive enumeration (sum_r P(n,r) ~= 13,700) cheap.
        rng = random.Random(20261005)
        cases = 0
        for _ in range(250):
            n = rng.randint(0, 7)
            items = [rng.choice("abcd") for _ in range(n)]
            for r in range(0, n + 2):
                got_c = list(unique_combinations(items, r))
                self.assertEqual(len(got_c), len(set(got_c)),
                                 "duplicate combinations emitted")
                self.assertEqual(set(got_c), naive_combinations(items, r))

                got_p = list(unique_permutations(items, r))
                self.assertEqual(len(got_p), len(set(got_p)),
                                 "duplicate permutations emitted")
                self.assertEqual(set(got_p), naive_permutations(items, r))
                cases += 1
        print(f"\n[differential] {cases} (multiset, r) cases matched naive "
              f"enumerate-then-dedup exactly", flush=True)

    def test_heavy_duplicates(self):
        # n = 10, only 2 distinct values: naive enumeration is still cheap,
        # but duplicates are extreme (up to 6 identical copies).
        items = list("aaaaaabbbb")
        for r in range(0, 7):
            got_c = list(unique_combinations(items, r))
            self.assertEqual(len(got_c), len(set(got_c)))
            self.assertEqual(set(got_c), naive_combinations(items, r))
            got_p = list(unique_permutations(items, r))
            self.assertEqual(len(got_p), len(set(got_p)))
            self.assertEqual(set(got_p), naive_permutations(items, r))

    def test_medium_duplicates(self):
        items = list("aaabbbccc")
        for r in (0, 2, 4, 5, 7, 9):
            got_c = list(unique_combinations(items, r))
            self.assertEqual(len(got_c), len(set(got_c)))
            self.assertEqual(set(got_c), naive_combinations(items, r))
            got_p = list(unique_permutations(items, r))
            self.assertEqual(len(got_p), len(set(got_p)))
            self.assertEqual(set(got_p), naive_permutations(items, r))


class CountFormulaTest(unittest.TestCase):
    """Enumerated count must equal the closed-form / DP formula count."""

    # Combination counts stay small even for big multisets, so the
    # 20-element half-duplicate benchmark case is fully enumerable here.
    COMBO_CASES = [
        ("abcde", 3),
        ("aabbccddeeffgghhiijj", 10),   # 20 elements, half duplicates
        ("aaabbbccdddeee", 7),
        ("xxxxxx", 4),
        ("ab", 2),
        ("z", 1),
    ]

    # Permutation counts explode, so these stay fully enumerable.
    PERM_CASES = [
        ("abcde", 3),
        ("aabbcc", 4),
        ("aabbccdd", 8),                # full length: 8! / 2**4 = 2520
        ("aaabbbccc", 9),               # full length: 9! / (3!)**3 = 1680
        ("xxxxxx", 4),
        ("ab", 2),
        ("z", 1),
    ]

    def test_combination_counts(self):
        for items, r in self.COMBO_CASES:
            with self.subTest(items=items, r=r):
                self.assertEqual(sum(1 for _ in unique_combinations(items, r)),
                                 count_combinations(items, r))

    def test_permutation_counts(self):
        for items, r in self.PERM_CASES:
            with self.subTest(items=items, r=r):
                self.assertEqual(sum(1 for _ in unique_permutations(items, r)),
                                 count_permutations(items, r))

    def test_full_permutation_closed_form(self):
        # DP formula must agree with the independent closed form n!/prod(c_i!).
        rng = random.Random(7)
        for _ in range(50):
            items = [rng.choice("abcde") for _ in range(rng.randint(0, 12))]
            self.assertEqual(count_permutations(items),
                             full_permutation_count_closed_form(items))

    def test_combination_generating_function(self):
        # Total over all r of C(r) must equal prod(c_i + 1).
        items = list("aabbccdef")
        total = sum(count_combinations(items, r) for r in range(len(items) + 1))
        self.assertEqual(total, 3 * 3 * 3 * 2 * 2 * 2)


class EdgeCaseTest(unittest.TestCase):
    def test_no_duplicates(self):
        items = ["a", "b", "c", "d", "e"]
        self.assertEqual(len(list(unique_combinations(items, 3))), 10)
        self.assertEqual(len(list(unique_permutations(items, 3))), 60)

    def test_all_identical(self):
        items = ["x"] * 6
        self.assertEqual(list(unique_combinations(items, 4)),
                         [("x", "x", "x", "x")])
        self.assertEqual(list(unique_permutations(items, 4)),
                         [("x", "x", "x", "x")])
        self.assertEqual(count_combinations(items, 4), 1)
        self.assertEqual(count_permutations(items, 4), 1)

    def test_single_element(self):
        self.assertEqual(list(unique_combinations(["s"], 1)), [("s",)])
        self.assertEqual(list(unique_permutations(["s"], 1)), [("s",)])
        self.assertEqual(list(unique_combinations(["s"], 0)), [()])
        self.assertEqual(list(unique_combinations(["s"], 2)), [])

    def test_limit_terminates(self):
        items = list("aabbccddeeffgghhiijj")  # 20 elements, half duplicates
        limit = 500
        got = list(unique_permutations(items, limit=limit))
        self.assertEqual(len(got), limit)
        # The truncated stream must be an exact prefix of the full stream.
        prefix = list(itertools.islice(unique_permutations(items), limit))
        self.assertEqual(got, prefix)
        got_c = list(unique_combinations(items, 10, limit=limit))
        self.assertEqual(len(got_c), limit)
        self.assertEqual(got_c,
                         list(itertools.islice(unique_combinations(items, 10),
                                               limit)))

    def test_limit_zero(self):
        self.assertEqual(list(unique_permutations("abc", limit=0)), [])
        self.assertEqual(list(unique_combinations("abc", 2, limit=0)), [])


class MemoryBoundTest(unittest.TestCase):
    """Peak memory must stay bounded regardless of how many results stream by."""

    @staticmethod
    def _peak_bytes(consume):
        tracemalloc.start()
        consume()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return peak

    def test_streaming_memory_is_flat(self):
        items = list("aabbccddeeffgghhiijj")  # 20! / 2**10 ~= 2.4e15 permutations

        def drain(n):
            for _ in itertools.islice(unique_permutations(items), n):
                pass

        peak_small = self._peak_bytes(lambda: drain(20_000))
        peak_large = self._peak_bytes(lambda: drain(500_000))
        print(f"\n[memory] peak after 20k results:  {peak_small/1024:.1f} KiB"
              f"\n[memory] peak after 500k results: {peak_large/1024:.1f} KiB",
              flush=True)
        # Hard upper bound: 25x more results must not grow peak memory
        # (allow 64 KiB of slack for interpreter noise).
        self.assertLess(peak_large, peak_small + 64 * 1024)
        self.assertLess(peak_large, 1024 * 1024)  # absolute ceiling: 1 MiB

    def test_combination_streaming_memory(self):
        items = list("aabbccddeeffgghhiijj")

        def drain():
            for r in range(21):
                for _ in unique_combinations(items, r):
                    pass

        peak = self._peak_bytes(drain)
        print(f"\n[memory] peak draining all 59049 combinations: "
              f"{peak/1024:.1f} KiB", flush=True)
        self.assertLess(peak, 1024 * 1024)


if __name__ == "__main__":
    unittest.main(verbosity=2)

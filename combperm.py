"""Streaming duplicate-free combination/permutation enumeration for multisets.

Only the Python standard library is used.

Dedup rule
----------
A multiset is canonicalized into (value, multiplicity) pairs sorted by value.

* Combinations: every r-combination of a multiset corresponds bijectively to a
  multiplicity vector (k_1, ..., k_m) with 0 <= k_i <= count_i and
  sum(k_i) = r.  The enumerator enumerates each valid multiplicity vector
  exactly once (lexicographic DFS over the vector), therefore each distinct
  combination is emitted exactly once and no dedup pass is ever needed.

* Permutations: the enumerator does positional DFS; at each position it
  branches on *distinct values* whose remaining count is > 0 (never on
  identical copies).  Each distinct permutation corresponds to exactly one
  root-to-leaf path of value choices, so it is emitted exactly once.

Both enumerators are generators: only O(m + r) auxiliary state (the counts
table plus the current prefix) is kept, independent of the (possibly
astronomical) number of results.
"""

from collections import Counter
from math import comb, factorial

__all__ = [
    "unique_combinations",
    "unique_permutations",
    "count_combinations",
    "count_permutations",
]


class _Stop(Exception):
    """Internal unwinding signal used to implement ``limit``."""


def _pairs(items):
    pairs = sorted(Counter(items).items(), key=lambda p: p[0])
    values = [v for v, _ in pairs]
    counts = [c for _, c in pairs]
    return values, counts


def unique_combinations(items, r, limit=None):
    """Yield each distinct r-combination of ``items`` exactly once.

    ``items`` may contain duplicates; elements must be mutually comparable.
    Each result is a sorted tuple of length ``r``.  Enumeration is streaming:
    memory use is O(distinct + r), not O(number of results).

    ``limit`` (optional) caps the number of emitted results; enumeration
    stops immediately after the ``limit``-th result is yielded.
    """
    if r < 0:
        raise ValueError("r must be non-negative")
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if limit == 0:
        return
    values, counts = _pairs(items)
    m = len(values)
    suffix = [0] * (m + 1)  # suffix[i] = total copies available from slot i on
    for i in range(m - 1, -1, -1):
        suffix[i] = suffix[i + 1] + counts[i]
    if r > suffix[0]:
        return

    emitted = 0
    combo = []

    def dfs(i, remaining):
        nonlocal emitted
        if remaining == 0:
            yield tuple(combo)
            emitted += 1
            if limit is not None and emitted >= limit:
                raise _Stop
            return
        # k_i must leave enough room for slots after i and not exceed count_i.
        lo = max(0, remaining - suffix[i + 1])
        hi = min(counts[i], remaining)
        value = values[i]
        for k in range(lo, hi + 1):
            combo.extend([value] * k)
            yield from dfs(i + 1, remaining - k)
            del combo[len(combo) - k:]

    try:
        yield from dfs(0, r)
    except _Stop:
        return


def unique_permutations(items, r=None, limit=None):
    """Yield each distinct r-permutation of ``items`` exactly once.

    ``items`` may contain duplicates; elements must be mutually comparable.
    ``r`` defaults to the total number of elements (full permutations).
    Enumeration is streaming: memory use is O(distinct + r).

    ``limit`` (optional) caps the number of emitted results.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if limit == 0:
        return
    values, counts = _pairs(items)
    total = sum(counts)
    if r is None:
        r = total
    if r < 0:
        raise ValueError("r must be non-negative")
    if r > total:
        return

    m = len(values)
    emitted = 0
    perm = []

    def dfs(pos):
        nonlocal emitted
        if pos == r:
            yield tuple(perm)
            emitted += 1
            if limit is not None and emitted >= limit:
                raise _Stop
            return
        for j in range(m):
            if counts[j] == 0:
                continue
            counts[j] -= 1
            perm.append(values[j])
            yield from dfs(pos + 1)
            perm.pop()
            counts[j] += 1

    try:
        yield from dfs(0)
    except _Stop:
        return


def count_combinations(items, r):
    """Number of distinct r-combinations, by the generating-function formula.

    The count is the coefficient of x**r in prod_i (1 + x + ... + x**c_i),
    where c_i are the multiplicities; computed with a sliding-window DP in
    O(distinct * r) time and O(r) space.
    """
    if r < 0:
        raise ValueError("r must be non-negative")
    dp = [0] * (r + 1)
    dp[0] = 1
    for c in Counter(items).values():
        c = min(c, r)
        ndp = [0] * (r + 1)
        window = 0
        for s in range(r + 1):
            window += dp[s]
            if s - c - 1 >= 0:
                window -= dp[s - c - 1]
            ndp[s] = window
        dp = ndp
    return dp[r]


def count_permutations(items, r=None):
    """Number of distinct r-permutations, by the multinomial formula.

    The count is sum over all valid multiplicity vectors (k_1..k_m) with
    sum k_i = r of r! / prod(k_i!); computed as a DP that accumulates
    binomial factors C(s, k).  For full permutations (r == n) this equals
    the closed form n! / prod(c_i!).
    """
    counts = list(Counter(items).values())
    total = sum(counts)
    if r is None:
        r = total
    if r < 0:
        raise ValueError("r must be non-negative")
    if r > total:
        return 0
    dp = [0] * (r + 1)
    dp[0] = 1
    for c in counts:
        ndp = [0] * (r + 1)
        for s in range(r + 1):
            if dp[s] == 0:
                continue
            for k in range(min(c, r - s) + 1):
                ndp[s + k] += dp[s] * comb(s + k, k)
        dp = ndp
    return dp[r]


def full_permutation_count_closed_form(items):
    """n! / prod(c_i!) -- independent closed form used to cross-check the DP."""
    counts = list(Counter(items).values())
    result = factorial(sum(counts))
    for c in counts:
        result //= factorial(c)
    return result

"""Sliding-window median maintained incrementally with two heaps.

Standard library only.

Balancing convention
--------------------
``lo`` is a max-heap (negated values), ``hi`` is a min-heap. After every
settled window the invariant is::

    len(lo) == len(hi)            # even window size
    len(lo) == len(hi) + 1        # odd window size

and every element of ``lo`` is <= every element of ``hi``. The median is
therefore ``-lo[0]`` for odd windows, and the average of ``-lo[0]`` and
``hi[0]`` for even windows. The lower half owns the extra element.

Deletions are lazy. A global pending dict counts elements logically removed;
heap tops are pruned before they are inspected, so duplicates are handled by
count and never by identity. Dead elements buried inside a heap may never
reach the top on their own (e.g. old minima in a growing stream), so the
heaps are rebuilt from live elements once dead entries outnumber live ones.
That rebuild is O(k) and happens at most once per k steps, keeping the
amortized cost O(1) per step and the total memory O(k).
"""

from fractions import Fraction
from heapq import heapify, heappop, heappush
from typing import Hashable, Iterator, Sized

__all__ = ["SlidingWindowMedian", "sliding_window_medians"]


class SlidingWindowMedian:
    """Two-heap sliding-window median with lazy deletion."""

    def __init__(self) -> None:
        self._lo: list = []  # max-heap, values negated
        self._hi: list = []  # min-heap
        self._pending: dict = {}
        self._lo_size = 0
        self._hi_size = 0

    def __len__(self) -> int:
        return self._lo_size + self._hi_size

    def add(self, value: Hashable) -> None:
        """Insert one element, then restore the heap-size invariant."""
        if not self._lo or value <= -self._lo[0]:
            heappush(self._lo, -value)
            self._lo_size += 1
        else:
            heappush(self._hi, value)
            self._hi_size += 1
        self._rebalance()

    def remove(self, value: Hashable) -> None:
        """Lazily remove one occurrence of ``value``, then rebalance."""
        if self._lo_size + self._hi_size == 0:
            raise ValueError("remove from empty window")
        # Route by value against the live lo top; prune first so the top is live.
        self._prune(self._lo)
        self._prune(self._hi)
        if self._lo and value <= -self._lo[0]:
            self._lo_size -= 1
        else:
            self._hi_size -= 1
        self._pending[value] = self._pending.get(value, 0) + 1
        self._prune(self._lo)
        self._prune(self._hi)
        self._rebalance()
        if len(self._lo) + len(self._hi) > 2 * (self._lo_size + self._hi_size):
            self._rebuild()

    def median(self):
        """Current window median.

        Odd size: the lower-middle element (int/float/... as inserted).
        Even size: exact mean of the two middle elements as ``Fraction``.
        """
        if self._lo_size == 0 and self._hi_size == 0:
            raise ValueError("median of empty window")
        self._prune(self._lo)
        lower = -self._lo[0]
        if self._lo_size > self._hi_size:
            return lower
        self._prune(self._hi)
        upper = self._hi[0]
        return (Fraction(lower) + Fraction(upper)) / 2

    def _prune(self, heap: list) -> None:
        pending = self._pending
        while heap:
            raw = heap[0]
            value = -raw if heap is self._lo else raw
            count = pending.get(value)
            if count is None:
                break
            heappop(heap)
            if count == 1:
                # Delete zero-count keys so the pending map stays O(k).
                del pending[value]
            else:
                pending[value] = count - 1

    def _rebalance(self) -> None:
        if self._lo_size > self._hi_size + 1:
            heappush(self._hi, -heappop(self._lo))
            self._lo_size -= 1
            self._hi_size += 1
        elif self._lo_size < self._hi_size:
            heappush(self._lo, -heappop(self._hi))
            self._hi_size -= 1
            self._lo_size += 1
        # A moved element can land on a heap top as pending; prune tops now so
        # that any subsequent comparison against lo[0] sees a live element.
        self._prune(self._lo)
        self._prune(self._hi)

    def _rebuild(self) -> None:
        """Drop all pending (dead) entries by re-heapifying live elements.

        Runs when dead entries outnumber live ones, so its O(k) cost is
        amortized over at least k removals and physical heap size stays O(k).
        """
        pending = self._pending
        for heap, sign in ((self._lo, -1), (self._hi, 1)):
            live = []
            for raw in heap:
                value = sign * raw
                count = pending.get(value)
                if count is None:
                    live.append(raw)
                elif count == 1:
                    del pending[value]
                else:
                    pending[value] = count - 1
            heap[:] = live
            heapify(heap)


def sliding_window_medians(values: Sized, k: int) -> Iterator:
    """Yield the median of every length-``k`` window of ``values``."""
    if not isinstance(k, int):
        raise TypeError("k must be an int")
    n = len(values)
    if k <= 0:
        raise ValueError("k must be positive")
    if k > n:
        raise ValueError("window k larger than sequence length")
    window = SlidingWindowMedian()
    for i in range(n):
        window.add(values[i])
        if i >= k:
            window.remove(values[i - k])
        if i >= k - 1:
            yield window.median()

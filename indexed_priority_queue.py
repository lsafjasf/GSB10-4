"""Indexed binary min-heap with in-place key updates.

The heap stores unique items.  Each item has a priority key and a stable
insertion rank.  The rank is only used to make equal-key priorities total and
therefore deterministic.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")
K = TypeVar("K")


@dataclass(frozen=True)
class Swap:
    """A movement of the updated item during heap repair."""

    from_index: int
    to_index: int
    item: Any
    other_item: Any


@dataclass(frozen=True)
class UpdateReport:
    """Diagnostic information returned when ``update_key(..., trace=True)``."""

    item: Any
    old_key: Any
    new_key: Any
    start_index: int
    final_index: int
    direction: str
    swaps: tuple[Swap, ...]


class IndexedMinHeap(Generic[T, K]):
    """A binary min-heap supporting O(log n) priority updates.

    ``_heap`` stores items in breadth-first heap order.  ``_positions`` maps an
    item to its current heap index, giving O(1) location for an update.
    ``_keys`` stores the current key for each item.
    """

    def __init__(self, items: Iterable[tuple[T, K]] | None = None) -> None:
        self._heap: list[T] = []
        self._positions: dict[T, int] = {}
        self._keys: dict[T, K] = {}
        self._insertion_rank: dict[T, int] = {}
        self._next_rank = 0

        if items is not None:
            for item, key in items:
                self.push(item, key)

    def __len__(self) -> int:
        return len(self._heap)

    def __contains__(self, item: T) -> bool:
        return item in self._positions

    def __iter__(self) -> Iterator[T]:
        return iter(self._heap)

    def position(self, item: T) -> int:
        """Return the current heap index of ``item``."""

        try:
            return self._positions[item]
        except KeyError:
            raise KeyError(item) from None

    def get_key(self, item: T) -> K:
        """Return the current key associated with ``item``."""

        try:
            return self._keys[item]
        except KeyError:
            raise KeyError(item) from None

    def peek(self) -> tuple[T, K]:
        """Return the minimum item and key without removing them."""

        if not self._heap:
            raise IndexError("heap is empty")
        item = self._heap[0]
        return item, self._keys[item]

    def push(self, item: T, key: K) -> None:
        """Insert a unique item and restore the heap by sifting upward."""

        if item in self._positions:
            raise ValueError(f"item already exists: {item!r}")
        self._validate_key(key)

        index = len(self._heap)
        self._heap.append(item)
        self._positions[item] = index
        self._keys[item] = key
        self._insertion_rank[item] = self._next_rank
        self._next_rank += 1
        self._sift_up(index)

    def pop(self) -> tuple[T, K]:
        """Remove and return the minimum item and its key."""

        if not self._heap:
            raise IndexError("pop from empty heap")

        root_item = self._heap[0]
        root_key = self._keys[root_item]
        last_item = self._heap.pop()

        if self._heap:
            self._heap[0] = last_item
            self._positions[last_item] = 0
            self._sift_down(0)

        del self._positions[root_item]
        del self._keys[root_item]
        del self._insertion_rank[root_item]
        return root_item, root_key

    def update_key(
        self,
        item: T,
        new_key: K,
        *,
        trace: bool = False,
    ) -> UpdateReport | None:
        """Update a key and repair the heap in place.

        Location is O(1) through ``_positions[item]``.

        * Smaller key: the entry can only violate the parent edge, so sift up.
        * Larger key: the entry can only violate child edges, so sift down.
        * Equal key: no heap edge can become invalid, so do no swap.
        """

        if item not in self._positions:
            raise KeyError(item)
        self._validate_key(new_key)

        old_key = self._keys[item]
        start_index = self._positions[item]
        swaps: list[Swap] = []

        if new_key == old_key:
            direction = "none"
            final_index = start_index
        else:
            self._keys[item] = new_key
            if new_key < old_key:
                direction = "up"
                final_index = self._sift_up(start_index, swaps if trace else None)
            else:
                direction = "down"
                final_index = self._sift_down(start_index, swaps if trace else None)

        if not trace:
            return None
        return UpdateReport(
            item=item,
            old_key=old_key,
            new_key=new_key,
            start_index=start_index,
            final_index=final_index,
            direction=direction,
            swaps=tuple(swaps),
        )

    def check_invariants(self, *, raise_errors: bool = True) -> bool:
        """Validate mappings, uniqueness, and parent-child heap order."""

        errors: list[str] = []
        size = len(self._heap)

        if len(self._positions) != size:
            errors.append(f"position map size {len(self._positions)} != heap size {size}")
        if len(self._keys) != size:
            errors.append(f"key map size {len(self._keys)} != heap size {size}")
        if len(self._insertion_rank) != size:
            errors.append(
                f"rank map size {len(self._insertion_rank)} != heap size {size}"
            )

        seen: set[T] = set()
        for index, item in enumerate(self._heap):
            if item in seen:
                errors.append(f"duplicate item {item!r} at heap index {index}")
            seen.add(item)

            if self._positions.get(item) != index:
                errors.append(
                    f"item {item!r} at index {index} has position "
                    f"{self._positions.get(item)!r}"
                )
            if item not in self._keys:
                errors.append(f"item {item!r} has no key")
            if item not in self._insertion_rank:
                errors.append(f"item {item!r} has no insertion rank")

        for child in range(1, size):
            parent = (child - 1) // 2
            try:
                if self._less(child, parent):
                    errors.append(
                        f"heap order violated at child {child} and parent {parent}"
                    )
            except Exception as exc:
                errors.append(f"key comparison failed at {child}/{parent}: {exc}")

        if errors and raise_errors:
            raise AssertionError("invariant violation: " + "; ".join(errors))
        return not errors

    @staticmethod
    def _validate_key(key: K) -> None:
        if isinstance(key, float) and math.isnan(key):
            raise ValueError("NaN keys are not supported because they are unordered")

    def _less(self, left: int, right: int) -> bool:
        left_item = self._heap[left]
        right_item = self._heap[right]
        left_key = self._keys[left_item]
        right_key = self._keys[right_item]

        if left_key == right_key:
            return self._insertion_rank[left_item] < self._insertion_rank[right_item]
        return left_key < right_key

    def _swap(self, left: int, right: int) -> None:
        left_item = self._heap[left]
        right_item = self._heap[right]
        self._heap[left], self._heap[right] = right_item, left_item
        self._positions[left_item] = right
        self._positions[right_item] = left

    def _sift_up(
        self,
        start: int,
        trace: list[Swap] | None = None,
    ) -> int:
        current = start
        item = self._heap[current]

        while current > 0:
            parent = (current - 1) // 2
            if not self._less(current, parent):
                break

            parent_item = self._heap[parent]
            self._swap(current, parent)
            if trace is not None:
                trace.append(Swap(current, parent, item, parent_item))
            current = parent

        return current

    def _sift_down(
        self,
        start: int,
        trace: list[Swap] | None = None,
    ) -> int:
        current = start
        item = self._heap[current]
        size = len(self._heap)

        while True:
            left = current * 2 + 1
            right = left + 1
            smallest = current

            if left < size and self._less(left, smallest):
                smallest = left
            if right < size and self._less(right, smallest):
                smallest = right
            if smallest == current:
                break

            displaced_item = self._heap[smallest]
            self._swap(current, smallest)
            if trace is not None:
                trace.append(Swap(current, smallest, item, displaced_item))
            current = smallest

        return current

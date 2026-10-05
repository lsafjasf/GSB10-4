"""Indexed min-priority queue with in-place key updates.

Python 3, standard library only.

Design
------
- The heap is a 0-indexed array ``self._heap`` of entries ``[key, seq, item]``.
  Ordering is lexicographic on ``(key, seq)``: ``seq`` is a globally
  monotonically increasing insertion counter and is never reused, so equal
  keys never make two entries compare equal and no element can be lost or
  duplicated.
- ``self._pos`` maps each ``item`` to its current index in ``self._heap``.
  Locating an element for ``update_key`` is therefore an O(1) dict lookup;
  no scan of the heap is needed.
- After the key at index ``i`` is changed:
    * ``new_key < old_key`` -> sift *up* (the entry can only have become
      too large for its subtree, i.e. too small relative to its parent);
    * ``new_key > old_key`` -> sift *down* (the entry can only violate the
      order with its children);
    * ``new_key == old_key`` -> no movement; the heap is unchanged.
- ``_swap`` keeps ``self._pos`` synchronized with every swap performed by
  the sifts, so the position map is valid before and after every public
  operation.

Complexity: push/pop/update O(log n); positional lookup O(1).
"""


class Empty(Exception):
    pass


class IndexedPriorityQueue:
    def __init__(self):
        self._heap = []          # list of [key, seq, item]
        self._pos = {}           # item -> index in self._heap
        self._seq = 0            # global tie-breaker counter, never reused

    # ------------------------------------------------------------------ size
    def __len__(self):
        return len(self._heap)

    def __contains__(self, item):
        return item in self._pos

    def get_key(self, item):
        return self._heap[self._pos[item]][0]

    # ---------------------------------------------------------------- internals
    @staticmethod
    def _less(a, b):
        # compare by (key, seq); distinct seq values guarantee a total order
        return a[0] < b[0] or (a[0] == b[0] and a[1] < b[1])

    def _swap(self, i, j):
        hi, hj = self._heap[i], self._heap[j]
        self._heap[i], self._heap[j] = hj, hi
        self._pos[hi[2]] = j
        self._pos[hj[2]] = i

    def _sift_up(self, i):
        h = self._heap
        while i > 0:
            parent = (i - 1) // 2
            if not self._less(h[i], h[parent]):
                break
            self._swap(i, parent)
            i = parent

    def _sift_down(self, i):
        h = self._heap
        n = len(h)
        while True:
            left = 2 * i + 1
            if left >= n:
                break
            right = left + 1
            smallest = i
            if self._less(h[left], h[smallest]):
                smallest = left
            if right < n and self._less(h[right], h[smallest]):
                smallest = right
            if smallest == i:
                break
            self._swap(i, smallest)
            i = smallest

    # -------------------------------------------------------------- public API
    def push(self, item, key):
        if item in self._pos:
            raise ValueError(f"item already present: {item!r}")
        entry = [key, self._seq, item]
        self._seq += 1
        self._heap.append(entry)
        i = len(self._heap) - 1
        self._pos[item] = i
        self._sift_up(i)

    def peek(self):
        if not self._heap:
            raise Empty("priority queue is empty")
        key, _seq, item = self._heap[0]
        return item, key

    def pop(self):
        if not self._heap:
            raise Empty("priority queue is empty")
        root = self._heap[0]
        last = self._heap.pop()
        if self._heap:
            self._heap[0] = last
            self._pos[last[2]] = 0
            self._sift_down(0)
        del self._pos[root[2]]
        return root[2], root[0]

    def update_key(self, item, new_key):
        """Update the key of an existing item.

        Returns the old key. Adjustment path:
          new_key < old_key : sift up
          new_key > old_key : sift down
          new_key == old_key: no movement
        """
        i = self._pos[item]                 # O(1) position lookup
        entry = self._heap[i]
        old_key = entry[0]
        if new_key == old_key:
            return old_key                  # nothing can violate the invariant
        entry[0] = new_key
        if new_key < old_key:
            self._sift_up(i)
        else:
            self._sift_down(i)
        return old_key

    # ------------------------------------------------- invariant self-checking
    def check_invariant(self):
        """Validate every invariant of the structure.

        1. Heap order: for every non-root index i, heap[(i-1)//2] <= heap[i]
           under the (key, seq) ordering.
        2. Position map completeness: len(_pos) == len(_heap), every item
           appears exactly once.
        3. Position map correctness: _pos[item] == actual index.
        4. Sequence uniqueness: no two live entries share a seq.
        Raises AssertionError describing the first violation found.
        """
        h = self._heap
        n = len(h)
        assert len(self._pos) == n, \
            f"pos map size {len(self._pos)} != heap size {n}"
        seen_seq = set()
        for i, entry in enumerate(h):
            key, seq, item = entry
            if i > 0:
                parent = h[(i - 1) // 2]
                assert not self._less(entry, parent), \
                    f"heap order violated: child index {i} < parent"
            assert item in self._pos, f"item {item!r} missing from pos map"
            assert self._pos[item] == i, \
                f"pos map for {item!r} -> {self._pos[item]}, actual {i}"
            assert seq not in seen_seq, f"duplicate seq {seq}"
            seen_seq.add(seq)
        return True

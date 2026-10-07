"""Dynamic table with RFC 7541 section 4 semantics.

Rules (identical on encoder and decoder, so both ends stay in sync):

* Entry size = len(name) + len(value) + 32 bytes (per-entry overhead).
* Table size = sum of entry sizes; it must never exceed the current
  dynamic capacity, which itself must never exceed the protocol
  maximum (the peer's SETTINGS upper bound).
* Insert: the new entry is added at the FRONT (it becomes the newest
  entry).  Before inserting, entries are evicted from the BACK
  (oldest first) until the new entry fits.  If the entry alone is
  larger than the capacity, the table is emptied and the entry is
  NOT inserted (RFC 7541 section 4.4).
* Eviction: oldest entry first, only as many as needed.
* Capacity update: any value in [0, max_capacity] is allowed and may
  trigger evictions; a value above max_capacity is rejected with
  CapacityLimitExceeded and the table is left untouched.
* Indexing: absolute index 0 is the first entry ever inserted and
  grows monotonically; relative index 0 is the newest entry.
  Entries with absolute index < dropped_count have been evicted and
  can no longer be referenced.
"""

from .errors import (
    CapacityLimitExceeded,
    EvictedEntryReference,
    IndexOutOfRange,
    TableInvariantViolation,
)

ENTRY_OVERHEAD = 32


def entry_size(name, value):
    """Size of one table entry in bytes (RFC 7541 section 4.1)."""
    return (
        len(name.encode("utf-8"))
        + len(value.encode("utf-8"))
        + ENTRY_OVERHEAD
    )


class DynamicTable:
    def __init__(self, max_capacity):
        if max_capacity < 0:
            raise ValueError("max_capacity must be >= 0")
        self.max_capacity = max_capacity  # protocol upper bound
        self.capacity = max_capacity      # current dynamic capacity
        self._entries = []                # newest first (front = index 0)
        self.size = 0                     # current table size in bytes
        self.insert_count = 0             # total entries ever inserted
        self.dropped_count = 0            # total entries ever evicted
        self.last_evicted = []            # entries evicted by last mutation

    def __len__(self):
        return len(self._entries)

    @property
    def max_entries(self):
        """Max number of smallest-possible entries that fit (RFC 9204)."""
        return self.max_capacity // ENTRY_OVERHEAD

    def set_capacity(self, new_capacity):
        if new_capacity < 0 or new_capacity > self.max_capacity:
            raise CapacityLimitExceeded(
                "capacity update %d exceeds protocol maximum %d"
                % (new_capacity, self.max_capacity)
            )
        self.capacity = new_capacity
        self.last_evicted = []
        self._evict_to(self.capacity)
        self.check_invariant()

    def insert(self, name, value):
        """Insert an entry; returns False if it did not fit at all."""
        size = entry_size(name, value)
        self.last_evicted = []
        if size > self.capacity:
            # RFC 7541 section 4.4: evict everything, entry not added.
            self._evict_to(0)
            self.check_invariant()
            return False
        self._evict_to(self.capacity - size)
        self._entries.insert(0, (name, value))
        self.size += size
        self.insert_count += 1
        self.check_invariant()
        return True

    def _evict_to(self, target):
        while self.size > target and self._entries:
            name, value = self._entries.pop()  # oldest = back of list
            self.size -= entry_size(name, value)
            self.dropped_count += 1
            self.last_evicted.append((name, value))

    def get_absolute(self, abs_index):
        if abs_index < 0 or abs_index >= self.insert_count:
            raise IndexOutOfRange(
                "absolute index %d out of range (insert_count=%d)"
                % (abs_index, self.insert_count)
            )
        if abs_index < self.dropped_count:
            raise EvictedEntryReference(
                "absolute index %d already evicted (dropped_count=%d)"
                % (abs_index, self.dropped_count)
            )
        return self._entries[self.insert_count - 1 - abs_index]

    def get_relative(self, rel_index, base):
        """Pre-base relative index: rel 0 = entry with abs index base-1."""
        if rel_index < 0:
            raise IndexOutOfRange("negative relative index %d" % rel_index)
        return self.get_absolute(base - 1 - rel_index)

    def get_post_base(self, post_base_index, base):
        if post_base_index < 0:
            raise IndexOutOfRange(
                "negative post-base index %d" % post_base_index
            )
        return self.get_absolute(base + post_base_index)

    def find(self, name, value=None):
        """Absolute index of the newest matching entry, or None."""
        for pos, (entry_name, entry_value) in enumerate(self._entries):
            if entry_name == name and (value is None or entry_value == value):
                return self.insert_count - 1 - pos
        return None

    def check_invariant(self):
        if self.size > self.capacity:
            raise TableInvariantViolation(
                "table overflow: size %d > capacity %d"
                % (self.size, self.capacity)
            )
        if self.capacity > self.max_capacity:
            raise TableInvariantViolation(
                "capacity %d > max_capacity %d"
                % (self.capacity, self.max_capacity)
            )
        if len(self._entries) != self.insert_count - self.dropped_count:
            raise TableInvariantViolation(
                "entry count mismatch: %d entries, inserts=%d dropped=%d"
                % (len(self._entries), self.insert_count, self.dropped_count)
            )

    def snapshot(self):
        """Hashable, comparable view of the full table state."""
        return {
            "capacity": self.capacity,
            "size": self.size,
            "insert_count": self.insert_count,
            "dropped_count": self.dropped_count,
            "entries": tuple(
                (self.insert_count - 1 - pos, name, value)
                for pos, (name, value) in enumerate(self._entries)
            ),
        }

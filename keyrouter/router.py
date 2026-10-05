"""Key -> partition mapping.

Mapping rule (stable while num_partitions is unchanged):

    partition(key) = crc32(normalize(key)) % num_partitions

- crc32 (zlib.crc32) is deterministic across processes and machines,
  so the same key always lands on the same partition as long as the
  partition count does not change.
- normalize(): str is encoded as UTF-8; None / "" / b"" are mapped to a
  fixed sentinel byte string so empty keys are handled deterministically
  instead of crashing or being round-robined.
- crc32 has good avalanche behaviour, so keys that differ only slightly
  (e.g. "user:1" vs "user:2") are still spread across partitions.
"""

import zlib

EMPTY_KEY_SENTINEL = b"\x00__keyrouter_empty_key__\x00"


def normalize_key(key):
    if key is None:
        return EMPTY_KEY_SENTINEL
    if isinstance(key, str):
        key = key.encode("utf-8")
    if not isinstance(key, (bytes, bytearray)):
        raise TypeError("key must be str/bytes/None, got %r" % type(key))
    if len(key) == 0:
        return EMPTY_KEY_SENTINEL
    return bytes(key)


def hash_key(key):
    return zlib.crc32(normalize_key(key)) & 0xFFFFFFFF


def partition_for(key, num_partitions):
    if num_partitions < 1:
        raise ValueError("num_partitions must be >= 1")
    return hash_key(key) % num_partitions


class Router:
    def __init__(self, num_partitions):
        if num_partitions < 1:
            raise ValueError("num_partitions must be >= 1")
        self.num_partitions = num_partitions

    def partition_for(self, key):
        return partition_for(key, self.num_partitions)

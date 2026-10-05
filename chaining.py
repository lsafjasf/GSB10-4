"""链地址法哈希表：用于与开放地址法对拍，也作为性能参照。

* 桶数组 + 桶内 Python list 存 (key, value) 对。
* 负载因子（size/cap）超过 max_load（默认 1.0）时扩容一倍。
* 删除直接摘链，无墓碑问题。
"""


def _next_pow2(n: int) -> int:
    c = 8
    while c < n:
        c <<= 1
    return c


class ChainingHashTable:
    __slots__ = ("_cap", "_buckets", "_size", "_max_load", "_hash_fn")

    def __init__(self, capacity: int = 8, max_load: float = 1.0, hash_fn=None):
        self._cap = _next_pow2(max(8, capacity))
        self._buckets = [[] for _ in range(self._cap)]
        self._size = 0
        self._max_load = max_load
        self._hash_fn = hash_fn if hash_fn is not None else hash

    def __len__(self) -> int:
        return self._size

    @property
    def capacity(self) -> int:
        return self._cap

    @property
    def load_factor(self) -> float:
        return self._size / self._cap

    def _index(self, key) -> int:
        return self._hash_fn(key) & (self._cap - 1)

    def put(self, key, value) -> bool:
        if self._size + 1 > self._max_load * self._cap:
            self._rehash(self._cap * 2)
        b = self._buckets[self._index(key)]
        for idx, (k, _) in enumerate(b):
            if k == key:
                b[idx] = (key, value)
                return False
        b.append((key, value))
        self._size += 1
        return True

    def get(self, key, default=None):
        for k, v in self._buckets[self._index(key)]:
            if k == key:
                return v
        return default

    def __contains__(self, key) -> bool:
        return self.get(key, _SENTINEL) is not _SENTINEL

    def delete(self, key) -> bool:
        b = self._buckets[self._index(key)]
        for idx, (k, _) in enumerate(b):
            if k == key:
                b.pop(idx)
                self._size -= 1
                return True
        return False

    def items(self):
        for b in self._buckets:
            yield from b

    def _rehash(self, new_cap: int):
        old = self._buckets
        self._cap = new_cap
        self._buckets = [[] for _ in range(new_cap)]
        for b in old:
            for k, v in b:
                self._buckets[self._index(k)].append((k, v))


_SENTINEL = object()

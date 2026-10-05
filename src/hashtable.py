"""哈希表实现（仅标准库）。

提供两种实现，使用同一套键/操作即可对拍：

1. ProbingHashTable —— 开放地址法
   - 探测方式：双重散列（double hashing）
       h1(k) = h(k) mod m
       h2(k) = 1 | ((h(k) * φ mod 2^64) 的高 log2(m) 位)，强制为奇数
       探测序列：h1, h1+h2, h1+2*h2, ... (mod m)
     容量 m 恒为 2 的幂，步长恒为奇数 => gcd(h2, m)=1，探测序列在表满前
     遍历全部 m 个槽，不会出现子环。
      步长用 Fibonacci 散列（乘黄金比例常数 φ 取高位）：熵集中在低位
      （如 h(k)=k%16）或高位的弱哈希都能得到分散步长，避免退化为线性
      探测；对充分混合的默认哈希同样均匀，且只需一次乘法。
   - 删除：墓碑（tombstone）标记。失败查找只有遇到 EMPTY 才停止，
     墓碑只跳过，因此删除不会让查找失效。
   - 再散列维护（每次写操作后检查，摊还 O(1)）：
       a) 占用率 n/m > 0.70          -> 容量翻倍
       b) 墓碑率 t/m > 0.25          -> 同容量清理；若 n <= m/4 则减半
          （增长阈值 0.70 / 收缩阈值 0.25 构成迟滞，避免边界抖动）
       c) (n+t)/m >= 0.90            -> 同容量清理墓碑
   - 不变式（维护完成后）：n/m <= 0.70，t = 0。

2. ChainedHashTable —— 链地址法（对拍参照）
   容量 2 的幂，每桶一个 list，n/m >= 1 时翻倍。

hash_fn 约定：接收 key，返回任意整数（只用其低位）；默认实现对 int 做
SplitMix64 混合，对 str/bytes 做 FNV-1a 64。
"""

EMPTY = 0
OCCUPIED = 1
TOMBSTONE = 2

_MASK64 = (1 << 64) - 1
_MIN_CAPACITY = 8
_FNV_OFFSET = 1469598103934665603
_FNV_PRIME = 1099511628211


def splitmix64(x):
    x &= _MASK64
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9 & _MASK64
    x = (x ^ (x >> 27)) * 0x94D049BB133111EB & _MASK64
    return x ^ (x >> 31)


def default_hash(key):
    if isinstance(key, int):
        return splitmix64(key)
    if isinstance(key, str):
        data = key.encode("utf-8")
    elif isinstance(key, (bytes, bytearray)):
        data = bytes(key)
    else:
        data = repr(key).encode("utf-8")
    h = _FNV_OFFSET
    for b in data:
        h ^= b
        h = (h * _FNV_PRIME) & _MASK64
    return h


def _next_capacity(cap, n, max_load):
    while n > max_load * cap:
        cap *= 2
    return cap


def _odd_step(h, mask):
    # 奇数步长；容量为 2 的幂 => gcd(step, cap)=1，探测序列覆盖全表。
    # mask = cap - 1，返回值 ∈ [1, cap-1]。Fibonacci 散列取高 log2(cap) 位。
    return 1 | (((h & _MASK64) * 0x9E3779B97F4A7C15 & _MASK64)
                >> (64 - mask.bit_length()))


class ProbingHashTable:
    """双重散列 + 墓碑的开放地址哈希表。"""

    def __init__(self, initial_capacity=8, max_load=0.70,
                 max_tombstone_ratio=0.25, hash_fn=default_hash):
        cap = max(_MIN_CAPACITY, initial_capacity)
        cap = 1 << (cap - 1).bit_length()
        self._cap = cap
        self._mask = cap - 1
        self._max_load = max_load
        self._max_tomb_ratio = max_tombstone_ratio
        self._hash = hash_fn
        self._states = bytearray(cap)
        self._keys = [None] * cap
        self._values = [None] * cap
        self._size = 0
        self._tombstones = 0
        self.rehash_count = 0

    def __len__(self):
        return self._size

    def __contains__(self, key):
        return self._locate(key)[0] >= 0

    def capacity(self):
        return self._cap

    def tombstone_count(self):
        return self._tombstones

    def load_factor(self):
        return self._size / self._cap

    def _locate(self, key):
        """返回 (槽位, 第一块墓碑, 探测次数)；未找到时槽位为 -1。"""
        states = self._states
        keys = self._keys
        mask = self._mask
        h = self._hash(key)
        idx = h & mask
        step = _odd_step(h, mask)
        first_tomb = -1
        probes = 0
        while True:
            probes += 1
            state = states[idx]
            if state == EMPTY:
                return -1, first_tomb, probes
            if state == TOMBSTONE:
                if first_tomb < 0:
                    first_tomb = idx
            elif keys[idx] == key:
                return idx, first_tomb, probes
            idx = (idx + step) & mask

    def get(self, key, default=None):
        idx, _, _ = self._locate(key)
        return default if idx < 0 else self._values[idx]

    def __getitem__(self, key):
        idx, _, _ = self._locate(key)
        if idx < 0:
            raise KeyError(key)
        return self._values[idx]

    def __setitem__(self, key, value):
        states = self._states
        keys = self._keys
        mask = self._mask
        h = self._hash(key)
        idx = h & mask
        step = _odd_step(h, mask)
        first_tomb = -1
        while True:
            state = states[idx]
            if state == EMPTY:
                if first_tomb >= 0:
                    idx = first_tomb
                    self._tombstones -= 1
                states[idx] = OCCUPIED
                keys[idx] = key
                self._values[idx] = value
                self._size += 1
                self._maintain()
                return
            if state == TOMBSTONE:
                if first_tomb < 0:
                    first_tomb = idx
            elif keys[idx] == key:
                self._values[idx] = value
                return
            idx = (idx + step) & mask

    def pop(self, key, default=...):
        idx, _, _ = self._locate(key)
        if idx < 0:
            if default is ...:
                raise KeyError(key)
            return default
        old = self._values[idx]
        self._states[idx] = TOMBSTONE
        self._keys[idx] = None
        self._values[idx] = None
        self._size -= 1
        self._tombstones += 1
        self._maintain()
        return old

    def __delitem__(self, key):
        self.pop(key)

    def _maintain(self):
        cap = self._cap
        n = self._size
        t = self._tombstones
        if n > self._max_load * cap:
            target = _next_capacity(cap, n, self._max_load)
        elif t > self._max_tomb_ratio * cap:
            target = cap
            while target > _MIN_CAPACITY and n <= target // 4:
                target //= 2
        elif (n + t) >= 0.90 * cap:
            target = cap
        else:
            return
        if target != cap or t:
            self._rehash(target)

    def _rehash(self, newcap):
        old_states = self._states
        old_keys = self._keys
        old_values = self._values
        mask = newcap - 1
        states = bytearray(newcap)
        keys = [None] * newcap
        values = [None] * newcap
        for old_idx, state in enumerate(old_states):
            if state != OCCUPIED:
                continue
            key = old_keys[old_idx]
            h = self._hash(key)
            idx = h & mask
            step = _odd_step(h, mask)
            while states[idx] != EMPTY:
                idx = (idx + step) & mask
            states[idx] = OCCUPIED
            keys[idx] = key
            values[idx] = old_values[old_idx]
        self._cap = newcap
        self._mask = mask
        self._states = states
        self._keys = keys
        self._values = values
        self._tombstones = 0
        self.rehash_count += 1

    def probe_length(self, key):
        """返回 (探测槽位数, 是否命中)。未命中数到第一个 EMPTY 为止。"""
        idx, _, probes = self._locate(key)
        return probes, idx >= 0

    def keys(self):
        for idx, state in enumerate(self._states):
            if state == OCCUPIED:
                yield self._keys[idx]

    def items(self):
        for idx, state in enumerate(self._states):
            if state == OCCUPIED:
                yield self._keys[idx], self._values[idx]

    def __iter__(self):
        return self.keys()


_MISS = object()


class ProbingHashTableNoCleanup(ProbingHashTable):
    """删除后只留墓碑、从不在删除路径清理的对照实现（用于实验对比）。

    插入导致 n/m 超限时仍会增长（顺带清除墓碑），这是开放地址表能继续
    工作所需的最低维护。
    """

    def _maintain(self):
        cap = self._cap
        if self._size > self._max_load * cap:
            target = _next_capacity(cap, self._size, self._max_load)
            self._rehash(target)


class ChainedHashTable:
    """链地址法哈希表（对拍参照）。"""

    def __init__(self, initial_capacity=8, max_load=1.0, hash_fn=default_hash):
        cap = max(_MIN_CAPACITY, initial_capacity)
        cap = 1 << (cap - 1).bit_length()
        self._cap = cap
        self._mask = cap - 1
        self._max_load = max_load
        self._hash = hash_fn
        self._buckets = [None] * cap
        self._size = 0

    def __len__(self):
        return self._size

    def capacity(self):
        return self._cap

    def load_factor(self):
        return self._size / self._cap

    def __contains__(self, key):
        return self.get(key, _MISS) is not _MISS

    def get(self, key, default=None):
        bucket = self._buckets[self._hash(key) & self._mask]
        if bucket is None:
            return default
        for k, v in bucket:
            if k == key:
                return v
        return default

    def __getitem__(self, key):
        value = self.get(key, _MISS)
        if value is _MISS:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        idx = self._hash(key) & self._mask
        bucket = self._buckets[idx]
        if bucket is None:
            self._buckets[idx] = [(key, value)]
            self._size += 1
        else:
            for i, (k, _) in enumerate(bucket):
                if k == key:
                    bucket[i] = (key, value)
                    return
            bucket.append((key, value))
            self._size += 1
        if self._size > self._max_load * self._cap:
            self._rehash(self._cap * 2)

    def pop(self, key, default=...):
        idx = self._hash(key) & self._mask
        bucket = self._buckets[idx]
        if bucket is not None:
            for i, (k, v) in enumerate(bucket):
                if k == key:
                    del bucket[i]
                    self._size -= 1
                    return v
        if default is ...:
            raise KeyError(key)
        return default

    def __delitem__(self, key):
        self.pop(key)

    def _rehash(self, newcap):
        old = self._buckets
        mask = newcap - 1
        buckets = [None] * newcap
        for bucket in old:
            if not bucket:
                continue
            for key, value in bucket:
                idx = self._hash(key) & mask
                if buckets[idx] is None:
                    buckets[idx] = [(key, value)]
                else:
                    buckets[idx].append((key, value))
        self._buckets = buckets
        self._cap = newcap
        self._mask = mask

    def comparison_count(self, key):
        """链地址法的等价指标：桶内键比较次数（空桶=0）。"""
        bucket = self._buckets[self._hash(key) & self._mask]
        if bucket is None:
            return 0, False
        for i, (k, _) in enumerate(bucket):
            if k == key:
                return i + 1, True
        return len(bucket), False

    def keys(self):
        for bucket in self._buckets:
            if bucket:
                for k, _ in bucket:
                    yield k

    def items(self):
        for bucket in self._buckets:
            if bucket:
                yield from bucket

    def __iter__(self):
        return self.keys()

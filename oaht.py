"""开放地址法哈希表：双重哈希探测 + 墓碑删除 + 动态再散列。

设计要点
--------
* 容量恒为 2 的幂 cap，掩码取模。
* 哈希混合：对 hash_fn(key) 做 splitmix64 风格的 64 位终结混合，
  避免 Python 小整数 hash(x)==x 导致第二哈希退化为 0。
* 探测序列（双重哈希）：
      h_i = (h1 + i * step) mod cap,  i = 0, 1, 2, ...
      h1   = mix & (cap-1)
      step = ((mix >> 32) & (cap-1)) | 1      # 恒为奇数
  cap 为 2 的幂且 step 为奇数 => gcd(step, cap)=1，
  序列在 cap 步内遍历全部槽位（排列），无二次聚集；
  不同键的 step 不同，避免线性探测的一次聚集。
* 删除使用墓碑（TOMBSTONE）：查找遇到墓碑继续探测，遇到 EMPTY 才停止，
  因此删除不会弄断既有键的探测链。
* 再散列触发条件：
    (a) 插入后 (size + tombstones) / cap > max_load（默认 0.7）
        —— 墓碑占用探测序列，必须计入"有效占用"。
    (b) 删除后 tombstones > size 且 tombstones > cap/8
        —— 防止纯删除工作负载下墓碑堆积导致未命中查找变慢。
  再散列时按 size / (max_load * 0.5) 选新容量（2 的幂，最小 8），
  一次再散列同时完成"扩容"与"清理墓碑"，摊还 O(1)。

负载因子上界依据（均匀/双重哈希探测的理论值，Knuth TAOCP vol.3）：
    命中查找期望探长 E_hit  ≈ (1/a) * ln(1/(1-a))
    未命中查找期望探长 E_miss ≈ 1/(1-a)
      a=0.50 -> 1.39 / 2.00
      a=0.70 -> 1.72 / 3.33   <- 默认值：时间与内存的折中
      a=0.90 -> 2.56 / 10.0
  超过 0.7 后未命中探长加速恶化，而内存收益递减，故取 0.7。
"""

_MASK64 = (1 << 64) - 1

_EMPTY, _FULL, _TOMB = 0, 1, 2


def _mix64(h: int) -> int:
    """splitmix64 终结混合，把低位信息扩散到高位。"""
    h &= _MASK64
    h ^= h >> 33
    h = (h * 0xFF51AFD7ED558CCD) & _MASK64
    h ^= h >> 33
    h = (h * 0xC4CEB9FE1A85EC53) & _MASK64
    h ^= h >> 33
    return h


def _next_pow2(n: int) -> int:
    c = 8
    while c < n:
        c <<= 1
    return c


class OpenAddressingHashTable:
    """开放地址法哈希表（双重哈希探测，墓碑删除）。"""

    __slots__ = (
        "_cap", "_keys", "_vals", "_state", "_size", "_tomb",
        "_max_load", "_hash_fn", "_collect", "_naive",
        "probe_hist", "max_probe", "rehash_count",
    )

    def __init__(self, capacity: int = 8, max_load: float = 0.7,
                 hash_fn=None, collect_stats: bool = False, naive: bool = False):
        if not 0.1 <= max_load < 1.0:
            raise ValueError("max_load must be in [0.1, 1.0)")
        self._cap = _next_pow2(max(8, capacity))
        self._keys = [None] * self._cap
        self._vals = [None] * self._cap
        self._state = bytearray(self._cap)          # 全为 _EMPTY
        self._size = 0
        self._tomb = 0
        self._max_load = max_load
        self._hash_fn = hash_fn if hash_fn is not None else hash
        self._collect = collect_stats
        self._naive = naive  # True=朴素对照：插入只按 size 扩容、删除不清理墓碑
        self.probe_hist = {}                         # 探长 -> 次数（get 统计）
        self.max_probe = 0
        self.rehash_count = 0

    # ---- 基本属性 -------------------------------------------------------
    def __len__(self) -> int:
        return self._size

    @property
    def capacity(self) -> int:
        return self._cap

    @property
    def load_factor(self) -> float:
        return self._size / self._cap

    @property
    def tombstones(self) -> int:
        return self._tomb

    # ---- 探测 -----------------------------------------------------------
    def _hashes(self, key):
        mix = _mix64(self._hash_fn(key))
        mask = self._cap - 1
        h1 = mix & mask
        step = ((mix >> 32) & mask) | 1             # 奇数，保证遍历全表
        return h1, step

    def _find(self, key):
        """返回 (index, found, first_tombstone)。同时记录本次探长。"""
        h1, step = self._hashes(key)
        mask = self._cap - 1
        keys, state = self._keys, self._state
        i = h1
        probes = 0
        first_tomb = -1
        while True:
            probes += 1
            st = state[i]
            if st == _EMPTY:
                self._note_probe(probes)
                return i, False, first_tomb
            if st == _FULL and keys[i] == key:
                self._note_probe(probes)
                return i, True, first_tomb
            if st == _TOMB and first_tomb < 0:
                first_tomb = i
            i = (i + step) & mask

    def _note_probe(self, probes: int):
        if probes > self.max_probe:
            self.max_probe = probes
        if self._collect:
            h = self.probe_hist
            h[probes] = h.get(probes, 0) + 1

    # ---- 增删查 ---------------------------------------------------------
    def put(self, key, value) -> bool:
        """插入或覆盖。返回 True 表示新插入。"""
        i, found, first_tomb = self._find(key)
        if found:
            self._vals[i] = value
            return False
        if first_tomb >= 0:
            i = first_tomb
            self._tomb -= 1
        self._keys[i] = key
        self._vals[i] = value
        self._state[i] = _FULL
        self._size += 1
        occupied = self._size if self._naive else self._size + self._tomb
        if occupied > self._max_load * self._cap:
            self._rehash(self._target_capacity())
        return True

    def get(self, key, default=None):
        i, found, _ = self._find(key)
        return self._vals[i] if found else default

    def __getitem__(self, key):
        i, found, _ = self._find(key)
        if not found:
            raise KeyError(key)
        return self._vals[i]

    def __setitem__(self, key, value):
        self.put(key, value)

    def __contains__(self, key) -> bool:
        _, found, _ = self._find(key)
        return found

    def delete(self, key) -> bool:
        i, found, _ = self._find(key)
        if not found:
            return False
        self._state[i] = _TOMB
        self._keys[i] = None
        self._vals[i] = None
        self._size -= 1
        self._tomb += 1
        # 删除侧清理：墓碑多于有效元素且超过容量 1/8 时再散列，
        # 既清墓碑又顺势缩容，保证未命中查找不会随删除次数退化。
        if (not self._naive and self._tomb > self._size
                and self._tomb > self._cap // 8):
            self._rehash(self._target_capacity())
        return True

    # ---- 再散列 ---------------------------------------------------------
    def _target_capacity(self) -> int:
        # 目标：再散列后负载因子 <= max_load/2，给后续插入留余量
        need = int(self._size / (self._max_load * 0.5)) + 1
        return _next_pow2(max(8, need))

    def _rehash(self, new_cap: int):
        old_keys, old_vals, old_state = self._keys, self._vals, self._state
        self._cap = new_cap
        self._keys = [None] * new_cap
        self._vals = [None] * new_cap
        self._state = bytearray(new_cap)
        self._tomb = 0
        self.rehash_count += 1
        mask = new_cap - 1
        keys, state = self._keys, self._state
        for k, v, st in zip(old_keys, old_vals, old_state):
            if st != _FULL:
                continue
            mix = _mix64(self._hash_fn(k))
            i = mix & mask
            step = ((mix >> 32) & mask) | 1
            while state[i] == _FULL:
                i = (i + step) & mask
            state[i] = _FULL
            keys[i] = k
            self._vals[i] = v

    # ---- 遍历与统计 -----------------------------------------------------
    def items(self):
        for k, v, st in zip(self._keys, self._vals, self._state):
            if st == _FULL:
                yield k, v

    def probe_percentiles(self, ps=(0.5, 0.9, 0.99, 0.999)):
        """由 probe_hist 计算探长分位数，返回 {p: length} 及均值/最大值。"""
        hist = self.probe_hist
        total = sum(hist.values())
        if total == 0:
            return {}
        out = {"count": total,
               "mean": sum(k * v for k, v in hist.items()) / total,
               "max": max(hist)}
        for p in ps:
            rank = max(1, int(total * p + 0.999999))  # ceil
            acc = 0
            for k in sorted(hist):
                acc += hist[k]
                if acc >= rank:
                    out[p] = k
                    break
        return out

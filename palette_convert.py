"""调色板索引 <-> 真彩(RGB888) 互转库。

仅使用 Python 标准库。

核心 API
--------
- rgb_to_indexed(rgb, palette, *, metric, overflow, capacity) -> bytes
- indexed_to_rgb(indexed, palette) -> bytes
- error_stats(original, reconstructed) -> dict
- rgb_to_indexed_naive(rgb, palette, *, metric) -> bytes   # 朴素逐像素选色(对照用)

数据约定
--------
- 真彩图像: bytes, 长度 = 像素数 * 3, 每像素 (R, G, B), 分量均为 0..255。
- 索引图像: bytes, 每字节一个调色板下标。
- 调色板: (R, G, B) 三元组的序列。

选色依据 / 距离度量
-------------------
metric 可选:
- "euclidean" : 平方欧氏距离   dR^2 + dG^2 + dB^2
- "weighted"  : 加权平方距离   2*dR^2 + 4*dG^2 + 3*dB^2
                (按人眼对 G 最敏感、B 最不敏感加权, 整数权重)
- "redmean"   : redmean 近似感知距离(默认), 整数实现:
                (512+rmean)*dR^2 + 1024*dG^2 + (767-rmean)*dB^2,
                rmean=(R1+R2)//2; 该量为标准 redmean 公式乘 256 的整数版,
                只用于比较大小。

最近色搜索: 对调色板构建 3 维 KD 树(精确最近邻, 非近似),
并对已出现过的 RGB 做字典缓存(同一真彩色只搜索一次)。
KD 树剪枝使用各分量权重下界, 三种度量下都保证得到严格意义上的最近色。

超容量策略(overflow)
--------------------
- "quantize"(默认): 无论真彩色多少, 全部量化到调色板最近色, 永不抛错。
- "reject": 图像中不同颜色数超过 capacity(默认 256) 时抛 PaletteError,
            错误信息指出第几个像素引入了第 capacity+1 种新颜色。
绝不做 index % len(palette) 之类的取模映射。
"""


class PaletteError(ValueError):
    """调色板/索引数据非法, 或 reject 模式下颜色超容量时抛出。"""


_METRICS = {
    # name: (距离函数, 每个轴的权重下界 -- 用于 KD 树剪枝)
    "euclidean": (1, 1, 1),
    "weighted": (2, 4, 3),
    "redmean": (512, 1024, 512),
}


def _distance(metric):
    if metric == "euclidean":
        def dist(c, p):
            dr = c[0] - p[0]
            dg = c[1] - p[1]
            db = c[2] - p[2]
            return dr * dr + dg * dg + db * db
        return dist, _METRICS["euclidean"]
    if metric == "weighted":
        def dist(c, p):
            dr = c[0] - p[0]
            dg = c[1] - p[1]
            db = c[2] - p[2]
            return 2 * dr * dr + 4 * dg * dg + 3 * db * db
        return dist, _METRICS["weighted"]
    if metric == "redmean":
        def dist(c, p):
            dr = c[0] - p[0]
            dg = c[1] - p[1]
            db = c[2] - p[2]
            rmean = (c[0] + p[0]) >> 1
            return (512 + rmean) * dr * dr + 1024 * dg * dg + (767 - rmean) * db * db
        return dist, _METRICS["redmean"]
    raise ValueError(f"unknown metric {metric!r}, expected one of {sorted(_METRICS)}")


def validate_palette(palette, *, capacity=None):
    """校验调色板, 返回 [(r,g,b), ...]。非法时抛 PaletteError 并指出条目位置。"""
    entries = []
    for pos, entry in enumerate(palette):
        try:
            length = len(entry)
        except TypeError:
            raise PaletteError(
                f"palette entry {pos}: expected (R,G,B) triple, got {entry!r}"
            )
        if length != 3:
            raise PaletteError(
                f"palette entry {pos}: expected 3 components, got length {length}"
            )
        components = []
        for name, value in zip(("R", "G", "B"), entry):
            if isinstance(value, bool) or not isinstance(value, int):
                raise PaletteError(
                    f"palette entry {pos}: component {name}={value!r} is not an integer"
                )
            if not 0 <= value <= 255:
                raise PaletteError(
                    f"palette entry {pos}: component {name}={value} out of range 0..255"
                )
            components.append(value)
        entries.append((components[0], components[1], components[2]))
    if capacity is not None and len(entries) > capacity:
        raise PaletteError(
            f"palette has {len(entries)} entries, exceeds capacity {capacity}"
        )
    return entries


class _KDTree:
    """3 维 KD 树, 数组式存储, 迭代查询。"""

    __slots__ = ("root", "rgb", "pal", "axis", "left", "right")

    def __init__(self, entries):
        self.rgb = []
        self.pal = []
        self.axis = []
        self.left = []
        self.right = []
        items = [(rgb, i) for i, rgb in enumerate(entries)]
        self.root = self._build(items, 0)

    def _build(self, items, depth):
        if not items:
            return -1
        axis = depth % 3
        items.sort(key=lambda item: item[0][axis])
        mid = len(items) // 2
        node = len(self.axis)
        rgb, pal_index = items[mid]
        self.rgb.append(rgb)
        self.pal.append(pal_index)
        self.axis.append(axis)
        self.left.append(-1)
        self.right.append(-1)
        self.left[node] = self._build(items[:mid], depth + 1)
        self.right[node] = self._build(items[mid + 1:], depth + 1)
        return node

    def nearest(self, color, dist, min_weight):
        """返回 color 的最近色在调色板中的下标(平局时取搜索顺序先命中者)。"""
        rgb = self.rgb
        pal = self.pal
        axes = self.axis
        left = self.left
        right = self.right
        stack = [self.root]
        best_index = -1
        best_dist = None
        while stack:
            node = stack.pop()
            if node < 0:
                continue
            point = rgb[node]
            d = dist(color, point)
            if best_dist is None or d < best_dist:
                best_dist = d
                best_index = pal[node]
            axis = axes[node]
            delta = color[axis] - point[axis]
            if delta < 0:
                near, far = left[node], right[node]
            else:
                near, far = right[node], left[node]
            if near >= 0:
                stack.append(near)
            # 远侧子树任意点距离 >= min_weight[axis] * delta^2,
            # 该下界不小于当前最优时安全剪枝。
            if far >= 0 and min_weight[axis] * delta * delta < best_dist:
                stack.append(far)
        return best_index


def rgb_to_indexed(rgb, palette, *, metric="redmean", overflow="quantize", capacity=256):
    """真彩 bytes -> 索引 bytes。

    metric:   euclidean / weighted / redmean
    overflow: quantize(量化到最近色) / reject(颜色数超 capacity 时拒绝)
    """
    if overflow not in ("quantize", "reject"):
        raise ValueError(f"unknown overflow policy {overflow!r}, use 'quantize' or 'reject'")
    if isinstance(rgb, (bytes, bytearray, memoryview)):
        rgb = bytes(rgb)
    else:
        raise PaletteError("rgb must be bytes of RGB triplets")
    if len(rgb) % 3:
        raise PaletteError(
            f"rgb byte length {len(rgb)} is not a multiple of 3 (truncated pixel)"
        )
    entries = validate_palette(palette, capacity=capacity)
    if not entries:
        if rgb:
            raise PaletteError("palette is empty: cannot map non-empty image")
        return b""

    dist, min_weight = _distance(metric)
    tree = _KDTree(entries)
    cache = {}
    get = cache.get
    out = bytearray(len(rgb) // 3)
    pos = 0
    pixel_no = 0
    for i in range(0, len(rgb), 3):
        key = (rgb[i] << 16) | (rgb[i + 1] << 8) | rgb[i + 2]
        index = get(key)
        if index is None:
            if overflow == "reject" and len(cache) >= capacity:
                raise PaletteError(
                    f"pixel {pixel_no}: image has more than {capacity} distinct colors "
                    f"(overflow='reject'); new color ({rgb[i]},{rgb[i + 1]},{rgb[i + 2]})"
                )
            index = tree.nearest((rgb[i], rgb[i + 1], rgb[i + 2]), dist, min_weight)
            cache[key] = index
        out[pos] = index
        pos += 1
        pixel_no += 1
    return bytes(out)


def rgb_to_indexed_naive(rgb, palette, *, metric="redmean"):
    """朴素逐像素线性扫描选色(无 KD 树、无缓存), 仅作正确性/耗时对照。"""
    rgb = bytes(rgb)
    if len(rgb) % 3:
        raise PaletteError(
            f"rgb byte length {len(rgb)} is not a multiple of 3 (truncated pixel)"
        )
    entries = validate_palette(palette)
    if not entries:
        if rgb:
            raise PaletteError("palette is empty: cannot map non-empty image")
        return b""

    dist, _ = _distance(metric)
    out = bytearray(len(rgb) // 3)
    pos = 0
    n = len(entries)
    for i in range(0, len(rgb), 3):
        color = (rgb[i], rgb[i + 1], rgb[i + 2])
        best_index = 0
        best_dist = None
        for j in range(n):
            d = dist(color, entries[j])
            if best_dist is None or d < best_dist:
                best_dist = d
                best_index = j
        out[pos] = best_index
        pos += 1
    return bytes(out)


def indexed_to_rgb(indexed, palette):
    """索引 bytes -> 真彩 bytes。索引越界时抛 PaletteError 并指出像素位置。"""
    indexed = bytes(indexed)
    entries = validate_palette(palette)
    n = len(entries)
    out = bytearray(len(indexed) * 3)
    pos = 0
    for pixel_no, value in enumerate(indexed):
        if value >= n:
            raise PaletteError(
                f"pixel {pixel_no}: index {value} out of range 0..{n - 1} "
                f"(palette has {n} entries)"
            )
        r, g, b = entries[value]
        out[pos] = r
        out[pos + 1] = g
        out[pos + 2] = b
        pos += 3
    return bytes(out)


def error_stats(original, reconstructed):
    """两幅真彩 bytes 的逐通道误差统计。

    返回:
      pixels       像素数
      max_error    最大逐通道绝对误差 (0..255)
      mean_error   平均逐通道绝对误差 (对所有像素的 R/G/B 取均值)
      rms_error    逐通道均方根误差 RMSE
    """
    original = bytes(original)
    reconstructed = bytes(reconstructed)
    if len(original) != len(reconstructed):
        raise PaletteError(
            f"length mismatch: original {len(original)} vs reconstructed {len(reconstructed)}"
        )
    if len(original) % 3:
        raise PaletteError("image byte length is not a multiple of 3")
    if not original:
        return {"pixels": 0, "max_error": 0, "mean_error": 0.0, "rms_error": 0.0}

    def _diffs():
        # bytes 迭代产生 int, map 在 C 层完成逐通道相减
        return map(int.__sub__, original, reconstructed)

    max_error = max(map(abs, _diffs()))
    total = sum(map(abs, _diffs()))
    # 两个独立迭代器产生同一序列, 对应元素相乘即 d*d
    total_sq = sum(map(int.__mul__, _diffs(), _diffs()))
    channels = len(original)
    return {
        "pixels": channels // 3,
        "max_error": max_error,
        "mean_error": total / channels,
        "rms_error": (total_sq / channels) ** 0.5,
    }

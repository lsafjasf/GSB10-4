"""Palette <-> truecolor conversion library (Python 3, standard library only).

Features
--------
- truecolor (RGB bytes) <-> palette-indexed (1 byte/pixel) conversion.
- Nearest-color selection with an explicit, documented distance metric
  ("redmean" perceptual weighted Euclidean, or plain "euclidean").
- Overflow policy when an image has more unique colors than the palette
  capacity: adaptive quantization (median cut) or strict rejection.
  Never a modulo/hash mapping, so no banding jumps.
- Round-trip error statistics (max / mean per-channel absolute error).
- Strict validation: empty palette, malformed entries, wrong entry length
  and out-of-range indices are rejected with the offending position.
- Speed: exact-color cache + KD-tree over the palette, versus a naive
  per-pixel linear scan (used for the benchmark comparison).
"""

from __future__ import annotations

MAX_PALETTE_SIZE = 256  # one byte per index


class PaletteError(ValueError):
    """Invalid palette or indexed data. `position` locates the problem."""

    def __init__(self, message, position=None):
        self.position = position
        if position is not None:
            message = "%s (at %s)" % (message, position)
        super().__init__(message)


# ---------------------------------------------------------------- validation

def validate_palette(palette):
    """Normalize and validate a palette -> list of (r, g, b) tuples.

    Rejects: None/empty palette, >256 entries, entries that are not
    3-sequences, non-int or out-of-range channel values. Every error
    carries the position of the offending entry.
    """
    if palette is None:
        raise PaletteError("palette is None")
    try:
        n = len(palette)
    except TypeError:
        raise PaletteError("palette is not a sequence")
    if n == 0:
        raise PaletteError("palette is empty")
    if n > MAX_PALETTE_SIZE:
        raise PaletteError(
            "palette has %d entries, max is %d" % (n, MAX_PALETTE_SIZE))
    out = []
    for i, entry in enumerate(palette):
        pos = "palette entry %d" % i
        if entry is None:
            raise PaletteError("palette entry is missing (None)", pos)
        try:
            ch = tuple(entry)
        except TypeError:
            raise PaletteError("palette entry is not a sequence", pos)
        if len(ch) != 3:
            raise PaletteError(
                "palette entry must have exactly 3 channels, got %d"
                % len(ch), pos)
        for c in ch:
            if not isinstance(c, int) or isinstance(c, bool):
                raise PaletteError("channel %r is not an int" % (c,), pos)
            if c < 0 or c > 255:
                raise PaletteError("channel %d out of range 0..255" % c, pos)
        out.append((ch[0], ch[1], ch[2]))
    return out


def _check_rgb(rgb):
    n = len(rgb)
    if n % 3 != 0:
        raise PaletteError(
            "truecolor data length %d is not a multiple of 3" % n,
            position="byte %d" % (n - n % 3))
    return n // 3


# ------------------------------------------------------------------- metrics

def _dist_redmean(c, p):
    """Low-cost perceptual approximation (a.k.a. 'redmean').

    Weights the channels by eye sensitivity and by the mean red level:
        d^2 = (2 + r~/256) dr^2 + 4 dg^2 + (2 + (255 - r~)/256) db^2
    Channel weights are always >= 2, which the KD-tree uses as a safe
    lower bound for pruning.
    """
    dr = c[0] - p[0]
    dg = c[1] - p[1]
    db = c[2] - p[2]
    rm = (c[0] + p[0]) * 0.5
    return ((2.0 + rm / 256.0) * dr * dr + 4.0 * dg * dg
            + (2.0 + (255.0 - rm) / 256.0) * db * db)


def _dist_euclidean(c, p):
    dr = c[0] - p[0]
    dg = c[1] - p[1]
    db = c[2] - p[2]
    return dr * dr + dg * dg + db * db


_METRICS = {
    # name -> (distance fn, min per-axis weight used for KD-tree pruning)
    "redmean": (_dist_redmean, 2.0),
    "euclidean": (_dist_euclidean, 1.0),
}


def _get_metric(name):
    try:
        return _METRICS[name]
    except KeyError:
        raise PaletteError(
            "unknown metric %r, expected one of %s"
            % (name, sorted(_METRICS)))


# ------------------------------------------------------------------- KD-tree

class _KDNode(object):
    __slots__ = ("index", "axis", "left", "right")


class _KDTree(object):
    """Static 3-d KD-tree over palette entries for nearest-color queries."""

    __slots__ = ("points", "root")

    def __init__(self, points):
        self.points = points
        self.root = self._build(list(range(len(points))), 0)

    def _build(self, idxs, depth):
        if not idxs:
            return None
        axis = depth % 3
        pts = self.points
        idxs.sort(key=lambda i: pts[i][axis])
        mid = len(idxs) // 2
        node = _KDNode()
        node.index = idxs[mid]
        node.axis = axis
        node.left = self._build(idxs[:mid], depth + 1)
        node.right = self._build(idxs[mid + 1:], depth + 1)
        return node

    def nearest(self, color, distfn, min_weight):
        best_index = -1
        best_dist = float("inf")
        stack = [self.root]
        pts = self.points
        while stack:
            node = stack.pop()
            if node is None:
                continue
            p = pts[node.index]
            d = distfn(color, p)
            if d < best_dist or (d == best_dist and node.index < best_index):
                best_dist = d
                best_index = node.index
            axis = node.axis
            diff = color[axis] - p[axis]
            near, far = (node.left, node.right) if diff < 0 \
                else (node.right, node.left)
            if far is not None and min_weight * diff * diff <= best_dist:
                stack.append(far)
            if near is not None:
                stack.append(near)
        return best_index


def _nearest_linear(color, palette, distfn):
    best_i = 0
    best_d = float("inf")
    for i, p in enumerate(palette):
        d = distfn(color, p)
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


def nearest_index(color, palette, metric="redmean", tree=None):
    """Index of the palette entry nearest to `color` under `metric`.

    Selection rule: argmin over palette entries of the chosen distance;
    ties resolve to the lowest palette index.
    """
    pal = validate_palette(palette)
    distfn, min_w = _get_metric(metric)
    if tree is not None:
        return tree.nearest(tuple(color), distfn, min_w)
    return _nearest_linear(tuple(color), pal, distfn)


# ---------------------------------------------------------------- conversion

def truecolor_to_indexed(rgb, palette, metric="redmean"):
    """Convert RGB bytes (3 bytes/pixel) to index bytes (1 byte/pixel).

    Fast path: per-color memoization plus a KD-tree over the palette, so
    each *distinct* color costs ~log(k) instead of a full palette scan.
    """
    pal = validate_palette(palette)
    _check_rgb(rgb)
    distfn, min_w = _get_metric(metric)
    tree = _KDTree(pal) if len(pal) >= 16 else None
    cache = {}
    out = bytearray(len(rgb) // 3)
    j = 0
    for off in range(0, len(rgb), 3):
        key = (rgb[off] << 16) | (rgb[off + 1] << 8) | rgb[off + 2]
        idx = cache.get(key)
        if idx is None:
            color = (rgb[off], rgb[off + 1], rgb[off + 2])
            if tree is not None:
                idx = tree.nearest(color, distfn, min_w)
            else:
                idx = _nearest_linear(color, pal, distfn)
            cache[key] = idx
        out[j] = idx
        j += 1
    return bytes(out)


def truecolor_to_indexed_naive(rgb, palette, metric="redmean"):
    """Reference implementation: per-pixel linear scan, no cache, no tree.

    Used as the baseline for the speed comparison.
    """
    pal = validate_palette(palette)
    _check_rgb(rgb)
    distfn, _ = _get_metric(metric)
    out = bytearray(len(rgb) // 3)
    j = 0
    for off in range(0, len(rgb), 3):
        color = (rgb[off], rgb[off + 1], rgb[off + 2])
        out[j] = _nearest_linear(color, pal, distfn)
        j += 1
    return bytes(out)


def indexed_to_truecolor(indexed, palette):
    """Convert index bytes back to RGB bytes.

    Every index is validated; an out-of-range index is rejected with the
    pixel position of the first offender.
    """
    pal = validate_palette(palette)
    n = len(pal)
    if indexed:
        top = max(indexed)
        if top >= n:
            for pos, v in enumerate(indexed):
                if v >= n:
                    raise PaletteError(
                        "index %d out of range for palette of %d entries"
                        % (v, n), position="pixel %d" % pos)
    table = [bytes(p) for p in pal]
    return b"".join(table[i] for i in indexed)


# ----------------------------------------------------------------- histogram

def histogram(rgb):
    """{packed_rgb_int: pixel_count} for RGB bytes."""
    _check_rgb(rgb)
    hist = {}
    for off in range(0, len(rgb), 3):
        key = (rgb[off] << 16) | (rgb[off + 1] << 8) | rgb[off + 2]
        hist[key] = hist.get(key, 0) + 1
    return hist


# ------------------------------------------------------- palette generation

def _median_cut(hist, capacity):
    """Median-cut quantization of a color histogram to <= capacity colors."""
    items = [((p >> 16) & 255, (p >> 8) & 255, p & 255, c)
             for p, c in hist.items()]
    boxes = [items]
    while len(boxes) < capacity:
        # split the most populous splittable box
        best_i = -1
        best_pop = -1
        for i, box in enumerate(boxes):
            if len(box) < 2:
                continue
            pop = 0
            for it in box:
                pop += it[3]
            if pop > best_pop:
                best_pop = pop
                best_i = i
        if best_i < 0:
            break  # nothing splittable left
        box = boxes.pop(best_i)
        # longest channel wins the split axis
        mins = [min(it[k] for it in box) for k in range(3)]
        maxs = [max(it[k] for it in box) for k in range(3)]
        axis = max(range(3), key=lambda k: maxs[k] - mins[k])
        box.sort(key=lambda it: it[axis])
        half = best_pop // 2
        acc = 0
        cut = 0
        for cut, it in enumerate(box):
            acc += it[3]
            if acc >= half:
                break
        cut = max(cut, 1) if cut >= len(box) else cut + 1
        cut = min(max(cut, 1), len(box) - 1)
        boxes.append(box[:cut])
        boxes.append(box[cut:])
    palette = []
    for box in boxes:
        total = 0
        sr = sg = sb = 0
        for r, g, b, c in box:
            total += c
            sr += r * c
            sg += g * c
            sb += b * c
        palette.append((sr // total, sg // total, sb // total))
    return palette


def build_palette(rgb, capacity, strategy="median-cut"):
    """Build a palette of at most `capacity` entries for the image.

    Overflow policy (unique colors > capacity):
      - "median-cut": adaptive quantization; representative colors are
        population-weighted box averages (deterministic, no banding).
      - "reject": raise PaletteError instead of degrading the image.
    """
    if not isinstance(capacity, int) or isinstance(capacity, bool) \
            or capacity < 1 or capacity > MAX_PALETTE_SIZE:
        raise PaletteError(
            "capacity must be an int in 1..%d" % MAX_PALETTE_SIZE)
    hist = histogram(rgb)
    n_unique = len(hist)
    if n_unique <= capacity:
        return [((p >> 16) & 255, (p >> 8) & 255, p & 255)
                for p in sorted(hist)]
    if strategy == "reject":
        raise PaletteError(
            "image has %d unique colors, exceeds palette capacity %d; "
            "use strategy='median-cut' to quantize" % (n_unique, capacity))
    if strategy != "median-cut":
        raise PaletteError(
            "unknown overflow strategy %r, expected 'median-cut' or "
            "'reject'" % (strategy,))
    return _median_cut(hist, capacity)


# -------------------------------------------------------------- error stats

def roundtrip_error(rgb, palette, metric="redmean", hist=None):
    """Round-trip rgb -> indexed -> rgb and measure the per-channel error.

    Returns a dict with max_error (largest per-channel absolute error,
    0..255) and mean_error (mean per-channel absolute error). Errors are
    computed per distinct color weighted by its pixel count, which is
    exact and much faster than a per-pixel pass.
    """
    pal = validate_palette(palette)
    _check_rgb(rgb)
    if hist is None:
        hist = histogram(rgb)
    distfn, min_w = _get_metric(metric)
    tree = _KDTree(pal) if len(pal) >= 16 else None
    cache = {}
    max_err = 0
    total_err = 0
    total_px = 0
    for packed, cnt in hist.items():
        color = ((packed >> 16) & 255, (packed >> 8) & 255, packed & 255)
        idx = cache.get(packed)
        if idx is None:
            if tree is not None:
                idx = tree.nearest(color, distfn, min_w)
            else:
                idx = _nearest_linear(color, pal, distfn)
            cache[packed] = idx
        p = pal[idx]
        e0 = abs(color[0] - p[0])
        e1 = abs(color[1] - p[1])
        e2 = abs(color[2] - p[2])
        m = e0 if e0 >= e1 else e1
        if e2 > m:
            m = e2
        if m > max_err:
            max_err = m
        total_err += (e0 + e1 + e2) * cnt
        total_px += cnt
    mean_err = total_err / (total_px * 3.0) if total_px else 0.0
    return {
        "metric": metric,
        "pixels": total_px,
        "unique_colors": len(hist),
        "palette_size": len(pal),
        "max_error": max_err,
        "mean_error": mean_err,
    }

"""4096x4096 调色板转换耗时 / 往返误差基准。

用法: python3 benchmark.py

说明:
- 快速法 = KD 树最近邻 + 相同 RGB 缓存, 在完整 4096x4096(16,777,216 像素)上实测。
- 朴素法 = 每像素对全部 256 个调色板条目做线性扫描, 在 128/256/512 边长的
  同源裁剪图上实测, 再按像素数线性外推到 4096x4096 (朴素法每像素开销恒定)。
"""

import time

from palette_convert import (
    _distance,
    error_stats,
    indexed_to_rgb,
    rgb_to_indexed,
    rgb_to_indexed_naive,
)

W = H = 4096
BASE = 512          # 基础图尺寸, 再 8x 放大得到 4096x4096
SCALE = W // BASE
PALETTE = [(r, g, b)
           for r in range(0, 256, 51)
           for g in range(0, 256, 51)
           for b in range(0, 256, 51)]
PALETTE += [(v, v, v) for v in range(4, 256, 6)][:40]
assert len(PALETTE) == 256
METRIC = "redmean"


def make_base(size):
    """生成 size x size 确定性连续色调图(对角渐变 + 轻微抖动), 每分量量化到 32 级。"""
    row_bytes = []
    for y in range(size):
        gy = y * 255 // (size - 1)
        triplets = bytearray()
        for x in range(size):
            rx = x * 255 // (size - 1)
            bx = (x + y) * 255 // (2 * size - 2)
            n = ((x * 7 + y * 13) % 5) - 2
            r = min(255, max(0, (rx + n) // 8 * 8 + 4))
            g = min(255, max(0, (gy - n) // 8 * 8 + 4))
            b = min(255, max(0, (bx + n) // 8 * 8 + 4))
            triplets += bytes((r, g, b))
        row_bytes.append(bytes(triplets))
    return b"".join(row_bytes)


def upscale(base, size, scale):
    """最近邻放大 scale 倍 (base 为 size x size 的 RGB bytes)。"""
    rows = []
    for y in range(size):
        src_row = base[y * size * 3:(y + 1) * size * 3]
        wide = b"".join(src_row[x * 3:(x + 1) * 3] * scale for x in range(size))
        rows.append(wide * scale)
    return b"".join(rows)


def timed(label, fn, repeat=1):
    best = float("inf")
    result = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn()
        dt = time.perf_counter() - t0
        best = min(best, dt)
    print(f"  {label:<46} {best:10.3f} s")
    return best, result


def main():
    pixels = W * H
    print(f"Python {__import__('sys').version.split()[0]} | {W}x{H} = {pixels:,} 像素 "
          f"({pixels * 3 / 1e6:.1f} MB 真彩)")
    print(f"调色板: {len(PALETTE)} 色 (6x6x6 立方体 + 40 级灰度), 度量: {METRIC}\n")

    print("生成测试图 ...")
    t0 = time.perf_counter()
    base = make_base(BASE)
    image = upscale(base, BASE, SCALE)
    uniques = len({(base[i] << 16) | (base[i + 1] << 8) | base[i + 2]
                   for i in range(0, len(base), 3)})
    print(f"  基础图 {BASE}x{BASE} 8x 放大完成, 用时 {time.perf_counter() - t0:.1f}s, "
          f"不同颜色数 = {uniques:,}\n")

    print("[快速法] KD 树最近邻 + RGB 缓存 (4096x4096 实测)")
    t_fwd, indexed = timed("真彩 -> 索引", lambda: rgb_to_indexed(image, PALETTE, metric=METRIC))
    t_bwd, rebuilt = timed("索引 -> 真彩", lambda: indexed_to_rgb(indexed, PALETTE))
    t_stats, stats = timed("往返误差统计", lambda: error_stats(image, rebuilt))
    print(f"  合计                                        {t_fwd + t_bwd:10.3f} s\n")

    print(f"  往返误差: 最大 {stats['max_error']} / 255, "
          f"平均 {stats['mean_error']:.4f}, RMSE {stats['rms_error']:.4f}\n")

    dist, _ = _distance(METRIC)
    print("[朴素法] 逐像素线性扫描 256 条目 (小图实测)")
    sizes = []
    for side in (128, 256, 512):
        crop = image[:side * side * 3]
        t_naive, naive_idx = timed(f"{side}x{side} ({side * side:,} 像素)",
                                   lambda c=crop: rgb_to_indexed_naive(c, PALETTE, metric=METRIC))
        fast_idx = rgb_to_indexed(crop, PALETTE, metric=METRIC)
        for k, (a, b) in enumerate(zip(naive_idx, fast_idx)):
            color = (crop[3 * k], crop[3 * k + 1], crop[3 * k + 2])
            assert dist(color, PALETTE[a]) == dist(color, PALETTE[b])
        sizes.append((side, t_naive))
    per_pixel = sum(t / (s * s) for s, t in sizes) / len(sizes)
    print(f"  实测每像素平均耗时 {per_pixel * 1e6:.3f} us "
          f"(各尺寸外推值差异 < 5% 即视为线性)")
    t_naive_full = per_pixel * pixels
    print(f"\n[外推] 朴素法 4096x4096 预计耗时 {t_naive_full:.0f} s "
          f"({t_naive_full / 60:.1f} 分钟)")
    print(f"[对比] 快速法正向 {t_fwd:.3f} s, 加速比约 {t_naive_full / t_fwd:.0f}x "
          f"(含反向/统计总加速 {t_naive_full / (t_fwd + t_bwd + t_stats):.0f}x)")


if __name__ == "__main__":
    main()

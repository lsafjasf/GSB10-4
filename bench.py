"""Benchmark: 4096x4096 truecolor <-> palette conversion (stdlib only).

Naive baseline is measured on a 256x256 crop and extrapolated x256
(a full naive pass would take hours in pure Python); the optimized path
is measured on the full image.
"""

import time

from palette_lib import (build_palette, histogram, indexed_to_truecolor,
                         roundtrip_error, truecolor_to_indexed,
                         truecolor_to_indexed_naive)

W = H = 4096
CAPACITY = 256


def make_image(w, h):
    """Gradient-ish image built by tiling a 256x256 tile (65536 unique
    colors), so generation is fast but the color set is realistic."""
    tile_rows = []
    for y in range(256):
        row = bytearray()
        for x in range(256):
            row += bytes((x, y, (x * x + y * y) % 256))
        tile_rows.append(bytes(row))
    tile_row_full = [r * (w // 256) for r in tile_rows]
    return b"".join(tile_row_full[y % 256] for y in range(h))


def main():
    print("image: %dx%d RGB, palette capacity %d" % (W, H, CAPACITY))

    t0 = time.perf_counter()
    img = make_image(W, H)
    print("generate image      : %8.3f s (%d bytes)"
          % (time.perf_counter() - t0, len(img)))

    t0 = time.perf_counter()
    hist = histogram(img)
    print("histogram           : %8.3f s (%d unique colors)"
          % (time.perf_counter() - t0, len(hist)))

    t0 = time.perf_counter()
    pal = build_palette(img, CAPACITY)  # median-cut, histogram reused inside
    print("build palette (median-cut): %8.3f s (%d entries)"
          % (time.perf_counter() - t0, len(pal)))

    t0 = time.perf_counter()
    idx = truecolor_to_indexed(img, pal)
    t_opt = time.perf_counter() - t0
    print("truecolor->indexed (cache+KD-tree): %8.3f s" % t_opt)

    # naive baseline on a 256x256 crop, extrapolated x256
    crop = bytearray()
    for y in range(256):
        row = img[y * W * 3:(y + 1) * W * 3]
        crop += row[:256 * 3]
    t0 = time.perf_counter()
    idx_crop = truecolor_to_indexed_naive(bytes(crop), pal)
    t_naive_crop = time.perf_counter() - t0
    t_naive_full = t_naive_crop * (W * H) / (256 * 256)
    print("truecolor->indexed (naive, 256x256 crop): %8.3f s"
          % t_naive_crop)
    print("truecolor->indexed (naive, extrapolated to full): %8.1f s"
          % t_naive_full)
    print("speedup (optimized vs naive): ~%.0fx" % (t_naive_full / t_opt))

    # sanity: crop results agree
    opt_crop = bytearray()
    for y in range(256):
        opt_crop += idx[y * W:y * W + 256]
    assert bytes(opt_crop) == idx_crop, "optimized and naive disagree!"

    t0 = time.perf_counter()
    back = indexed_to_truecolor(idx, pal)
    print("indexed->truecolor  : %8.3f s" % (time.perf_counter() - t0))

    t0 = time.perf_counter()
    stats = roundtrip_error(img, pal, hist=hist)
    print("error stats         : %8.3f s" % (time.perf_counter() - t0))
    print("round-trip error: max=%d/255  mean=%.4f/255  "
          "(metric=%s, %d px, %d unique colors)"
          % (stats["max_error"], stats["mean_error"], stats["metric"],
             stats["pixels"], stats["unique_colors"]))


if __name__ == "__main__":
    main()

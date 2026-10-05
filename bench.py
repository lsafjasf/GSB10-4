"""Benchmarks: encoding choice table, space, encode/decode/ops timing on 1M bits.

Run:  python3 bench.py
"""

import random
import time

from bitmap_index import (
    ARRAY_THRESHOLD,
    CHUNK_BITS,
    CODEC_ARRAY,
    CODEC_BITMAP,
    BitmapIndex,
    encoded_size,
    select_codec,
)

N = 1_000_000
REPEAT = 3


def make_positions(density, seed):
    rng = random.Random(seed)
    if density <= 0.0:
        return []
    if density >= 1.0:
        return list(range(N))
    return sorted(rng.sample(range(N), int(N * density)))


def timed(fn, repeat=REPEAT):
    fn()  # warm-up
    best = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn()
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return result, best * 1000.0


def static_selection_table():
    print("=" * 72)
    print("Encoding selection table (per 65536-bit container)")
    print("=" * 72)
    print(f"{'cardinality':>11} {'density':>9} {'chosen':>7} {'ARRAY B':>9} "
          f"{'BITMAP B':>9} {'saved B':>9}")
    for card in [0, 1, 100, 1024, 4095, 4096, 4097, 8192, 32768, 65535, 65536]:
        codec = select_codec(card)
        chosen = "ARRAY" if codec == CODEC_ARRAY else "BITMAP"
        arr_b = encoded_size(card, CODEC_ARRAY)
        bmp_b = encoded_size(card, CODEC_BITMAP)
        saved = bmp_b - arr_b if codec == CODEC_ARRAY else arr_b - bmp_b
        print(f"{card:>11} {card / CHUNK_BITS:>9.4f} {chosen:>7} {arr_b:>9} "
              f"{bmp_b:>9} {saved:>9}")
    print(f"\nthreshold: ARRAY when cardinality <= {ARRAY_THRESHOLD} "
          f"(2*card <= 8192), else BITMAP\n")


def benchmark_distributions():
    densities = [0.0, 1e-5, 1e-4, 1e-3, 0.01, 0.05, 0.0625, 0.1, 0.5, 0.9, 0.99, 1.0]
    print("=" * 104)
    print(f"1,000,000-bit bitmaps (best of {REPEAT} runs, milliseconds)")
    print("=" * 104)
    print(f"{'density':>8} {'set bits':>9} {'#arr':>5} {'#bmp':>5} "
          f"{'bytes':>9} {'vs raw':>8} {'build ms':>9} {'enc ms':>8} {'dec ms':>8}")
    rows = []
    for d in densities:
        pos = make_positions(d, seed=42)
        idx, build_ms = timed(lambda: BitmapIndex.from_sorted_positions(pos), repeat=1)
        blob, enc_ms = timed(idx.encode)
        dec, dec_ms = timed(lambda: BitmapIndex.decode(blob))
        assert dec.encode() == blob
        n_arr = sum(1 for c in idx.codec_map().values() if c == CODEC_ARRAY)
        n_bmp = len(idx.containers) - n_arr
        raw = (N + 7) // 8
        tag = {0.0: "all-empty", 1.0: "all-full"}.get(d, "")
        print(f"{d:>8.5g} {len(pos):>9} {n_arr:>5} {n_bmp:>5} {len(blob):>9} "
              f"{len(blob) / raw:>7.2f}x {build_ms:>9.2f} {enc_ms:>8.2f} {dec_ms:>8.2f}  {tag}")
        rows.append((d, len(pos), n_arr, n_bmp, len(blob)))
    print(f"\nraw uncompressed bitmap: {(N + 7) // 8} bytes\n")
    return rows


def benchmark_codec_comparison():
    print("=" * 78)
    print("Both codecs on identical data: size + round-trip correctness")
    print("=" * 78)
    print(f"{'density':>8} {'adaptive B':>11} {'forced-ARRAY':>13} "
          f"{'forced-BITMAP':>14} {'decoded equal':>14}")
    for d in [1e-4, 1e-3, 0.01, 0.0625, 0.1, 0.5, 0.9]:
        pos = make_positions(d, seed=7)
        idx = BitmapIndex.from_sorted_positions(pos)
        ba = idx.forced(CODEC_ARRAY).encode()
        bb = idx.forced(CODEC_BITMAP).encode()
        ok = (BitmapIndex.decode(ba).to_positions()
              == BitmapIndex.decode(bb).to_positions() == pos)
        print(f"{d:>8.5g} {len(idx.encode()):>11} {len(ba):>13} "
              f"{len(bb):>14} {str(ok):>14}")
    print()


def benchmark_mixed_ops():
    print("=" * 96)
    print(f"Mixed-codec set operations (best of {REPEAT}, milliseconds)")
    print("=" * 96)
    print(f"{'A density':>9} {'A codec':>8} {'B density':>9} {'B codec':>8} "
          f"{'AND ms':>8} {'OR ms':>8} {'ANDNOT ms':>9}")
    cases = [
        (0.001, CODEC_ARRAY, 0.9, CODEC_BITMAP),
        (0.9, CODEC_BITMAP, 0.001, CODEC_ARRAY),
        (0.01, CODEC_ARRAY, 0.1, CODEC_BITMAP),
        (0.05, CODEC_BITMAP, 0.05, CODEC_BITMAP),
        (0.5, CODEC_BITMAP, 0.5, CODEC_BITMAP),
    ]
    for da, ca, db, cb in cases:
        a = BitmapIndex.from_sorted_positions(make_positions(da, 11)).forced(ca)
        b = BitmapIndex.from_sorted_positions(make_positions(db, 22)).forced(cb)
        (_, and_ms), (_, or_ms), (_, not_ms) = (
            timed(lambda: a & b), timed(lambda: a | b), timed(lambda: a.andnot(b))
        )
        ref = sorted(set(a.to_positions()) - set(b.to_positions()))
        assert a.andnot(b).to_positions() == ref
        print(f"{da:>9.4g} {('ARRAY' if ca == 0 else 'BITMAP'):>8} "
              f"{db:>9.4g} {('ARRAY' if cb == 0 else 'BITMAP'):>8} "
              f"{and_ms:>8.2f} {or_ms:>8.2f} {not_ms:>9.2f}")
    print()


def benchmark_edge_cases():
    print("=" * 60)
    print("Edge cases")
    print("=" * 60)
    cases = {
        "all-empty": [],
        "bit 0 only": [0],
        "bit 999999 only": [N - 1],
        "boundary pair": [65535, 65536],
        "all-full": list(range(N)),
    }
    for name, pos in cases.items():
        idx = BitmapIndex.from_sorted_positions(pos)
        blob = idx.encode()
        ok = BitmapIndex.decode(blob).to_positions() == pos
        print(f"{name:>14}: {len(blob):>7} bytes, roundtrip={ok}")
    print()


if __name__ == "__main__":
    static_selection_table()
    benchmark_distributions()
    benchmark_codec_comparison()
    benchmark_mixed_ops()
    benchmark_edge_cases()

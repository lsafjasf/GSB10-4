"""Self-tests and differential tests for bitmap_index (stdlib unittest).

Run:  python3 test_bitmap_index.py [-v]
"""

import random
import sys
import unittest

from bitmap_index import (
    ARRAY_THRESHOLD,
    BITMAP_BYTES,
    CHUNK_BITS,
    CODEC_ARRAY,
    CODEC_BITMAP,
    BitmapIndex,
    encoded_size,
    select_codec,
)

N = 1_000_000  # logical bitmap size used across tests
DENSITIES = [0.0, 1e-5, 1e-4, 1e-3, 0.01, 0.05, 0.0625, 0.1, 0.5, 0.9, 0.99, 1.0]


def make_positions(density, seed, n=N):
    rng = random.Random(seed)
    if density <= 0.0:
        return []
    if density >= 1.0:
        return list(range(n))
    k = int(n * density)
    return sorted(rng.sample(range(n), k))


def ref_and(a, b):
    return sorted(set(a) & set(b))


def ref_or(a, b):
    return sorted(set(a) | set(b))


def ref_andnot(a, b):
    return sorted(set(a) - set(b))


class TestSelectionRule(unittest.TestCase):
    def test_threshold_boundary(self):
        self.assertEqual(select_codec(0), CODEC_ARRAY)
        self.assertEqual(select_codec(1), CODEC_ARRAY)
        self.assertEqual(select_codec(ARRAY_THRESHOLD), CODEC_ARRAY)
        self.assertEqual(select_codec(ARRAY_THRESHOLD + 1), CODEC_BITMAP)
        self.assertEqual(select_codec(CHUNK_BITS), CODEC_BITMAP)

    def test_cost_crossover(self):
        # ARRAY costs 2*card bytes, BITMAP costs 8192; equal exactly at 4096.
        self.assertEqual(encoded_size(ARRAY_THRESHOLD, CODEC_ARRAY), BITMAP_BYTES)
        self.assertLess(encoded_size(ARRAY_THRESHOLD - 1, CODEC_ARRAY), BITMAP_BYTES)
        self.assertGreater(encoded_size(ARRAY_THRESHOLD + 1, CODEC_ARRAY), BITMAP_BYTES)

    def test_builder_follows_rule(self):
        for card in (0, 1, ARRAY_THRESHOLD, ARRAY_THRESHOLD + 1, CHUNK_BITS):
            idx = BitmapIndex.from_positions(range(card))
            if card == 0:
                self.assertEqual(idx.containers, {})
            else:
                self.assertEqual(idx.codec_map()[0], select_codec(card))


class TestRoundTrip(unittest.TestCase):
    def test_roundtrip_all_densities(self):
        for d in DENSITIES:
            for seed in (1, 2):
                pos = make_positions(d, seed)
                idx = BitmapIndex.from_sorted_positions(pos)
                back = BitmapIndex.decode(idx.encode())
                self.assertEqual(back.to_positions(), pos, f"density={d} seed={seed}")

    def test_codec_equivalence(self):
        """Same logical bitmap forced into either codec decodes identically."""
        for d in [1e-4, 1e-3, 0.01, 0.05, 0.1, 0.5, 0.9, 1.0]:
            pos = make_positions(d, seed=7)
            idx = BitmapIndex.from_sorted_positions(pos)
            as_array = idx.forced(CODEC_ARRAY)
            as_bitmap = idx.forced(CODEC_BITMAP)
            dec_a = BitmapIndex.decode(as_array.encode()).to_positions()
            dec_b = BitmapIndex.decode(as_bitmap.encode()).to_positions()
            self.assertEqual(dec_a, pos, f"ARRAY decode mismatch d={d}")
            self.assertEqual(dec_b, pos, f"BITMAP decode mismatch d={d}")
            self.assertEqual(dec_a, dec_b, f"codec mismatch d={d}")

    def test_decode_rejects_garbage(self):
        idx = BitmapIndex.from_positions([1, 2, 3])
        blob = bytearray(idx.encode())
        with self.assertRaises(ValueError):
            BitmapIndex.decode(b"XXXX" + bytes(blob[4:]))
        with self.assertRaises(ValueError):
            BitmapIndex.decode(bytes(blob[:-1]))  # truncated
        blob2 = bytearray(idx.encode())
        blob2[14] ^= 0xFF  # first array element becomes huge -> unsorted payload
        with self.assertRaises(ValueError):
            BitmapIndex.decode(bytes(blob2))


class TestMixedOpsDifferential(unittest.TestCase):
    """AND/OR/ANDNOT with all codec combinations vs a set() reference."""

    def _check(self, pa, pb, tag):
        ia = BitmapIndex.from_sorted_positions(pa)
        ib = BitmapIndex.from_sorted_positions(pb)
        # Force every codec combination: natural, array, bitmap.
        variants = {
            "natural": (ia, ib),
            "array": (ia.forced(CODEC_ARRAY), ib.forced(CODEC_ARRAY)),
            "bitmap": (ia.forced(CODEC_BITMAP), ib.forced(CODEC_BITMAP)),
            "mixed_ab": (ia.forced(CODEC_ARRAY), ib.forced(CODEC_BITMAP)),
            "mixed_ba": (ia.forced(CODEC_BITMAP), ib.forced(CODEC_ARRAY)),
        }
        exp_and, exp_or, exp_not = ref_and(pa, pb), ref_or(pa, pb), ref_andnot(pa, pb)
        for name, (x, y) in variants.items():
            self.assertEqual((x & y).to_positions(), exp_and, f"AND {tag}/{name}")
            self.assertEqual((x | y).to_positions(), exp_or, f"OR {tag}/{name}")
            self.assertEqual(x.andnot(y).to_positions(), exp_not, f"ANDNOT {tag}/{name}")
            # results must agree across codec combinations
            self.assertEqual((x & y).to_positions(), (ia & ib).to_positions())
            self.assertEqual((x | y).to_positions(), (ia | ib).to_positions())
            self.assertEqual(x.andnot(y).to_positions(), ia.andnot(ib).to_positions())

    def test_density_matrix(self):
        for da in [1e-4, 0.01, 0.05, 0.5, 0.9]:
            for db in [1e-4, 0.01, 0.5, 0.9]:
                pa = make_positions(da, seed=11)
                pb = make_positions(db, seed=22)
                self._check(pa, pb, f"a={da},b={db}")

    def test_random_seeds(self):
        for seed in range(5):
            pa = make_positions(0.004, seed=100 + seed)
            pb = make_positions(0.08, seed=200 + seed)
            self._check(pa, pb, f"seed={seed}")


class TestEdgeCases(unittest.TestCase):
    def test_empty(self):
        e = BitmapIndex()
        self.assertEqual(e.cardinality(), 0)
        self.assertEqual(e.to_positions(), [])
        self.assertEqual(BitmapIndex.decode(e.encode()).to_positions(), [])

    def test_full(self):
        full = BitmapIndex.from_sorted_positions(range(N))
        self.assertEqual(full.cardinality(), N)
        self.assertTrue(all(c == CODEC_BITMAP for c in full.codec_map().values()))
        self.assertEqual(BitmapIndex.decode(full.encode()).to_positions(), list(range(N)))

    def test_single_bit_boundaries(self):
        for p in (0, 1, 65535, 65536, 65537, N - 1):
            idx = BitmapIndex.from_positions([p])
            self.assertEqual(idx.to_positions(), [p])
            self.assertEqual(BitmapIndex.decode(idx.encode()).to_positions(), [p])

    def test_ops_with_empty(self):
        pos = make_positions(0.01, seed=3)
        idx, empty = BitmapIndex.from_sorted_positions(pos), BitmapIndex()
        self.assertEqual((idx & empty).to_positions(), [])
        self.assertEqual((idx | empty).to_positions(), pos)
        self.assertEqual(idx.andnot(empty).to_positions(), pos)
        self.assertEqual(empty.andnot(idx).to_positions(), [])

    def test_self_ops(self):
        pos = make_positions(0.2, seed=4)
        idx = BitmapIndex.from_sorted_positions(pos)
        self.assertEqual((idx & idx).to_positions(), pos)
        self.assertEqual((idx | idx).to_positions(), pos)
        self.assertEqual(idx.andnot(idx).to_positions(), [])

    def test_complement_pair(self):
        a = BitmapIndex.from_sorted_positions(range(0, N, 2))
        b = BitmapIndex.from_sorted_positions(range(1, N, 2))
        self.assertEqual((a & b).to_positions(), [])
        self.assertEqual((a | b).cardinality(), N)
        self.assertEqual(a.andnot(b).to_positions(), list(range(0, N, 2)))

    def test_result_renormalization(self):
        # OR of two sparse arrays crossing the threshold must flip to BITMAP.
        a = BitmapIndex.from_positions(range(3000))
        b = BitmapIndex.from_positions(range(2000, 5000))
        merged = a | b
        self.assertEqual(merged.cardinality(), 5000)
        self.assertEqual(merged.codec_map()[0], CODEC_BITMAP)
        # AND of two dense bitmaps falling below threshold must flip to ARRAY.
        c = BitmapIndex.from_positions(range(60000))
        d = BitmapIndex.from_positions(range(59000, 65536))
        inter = c & d
        # 60000-elems BITMAP & 6536-elems BITMAP -> 1000 elems -> ARRAY
        self.assertEqual(inter.cardinality(), 1000)
        self.assertEqual(inter.codec_map()[0], CODEC_ARRAY)
        e = BitmapIndex.from_positions(range(57000, 65536))
        inter2 = c & e
        # overlap 57000..59999 = 3000 elems -> ARRAY
        self.assertEqual(inter2.cardinality(), 3000)
        self.assertEqual(inter2.codec_map()[0], CODEC_ARRAY)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)

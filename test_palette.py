"""Self-tests for palette_lib (stdlib unittest). Run: python3 test_palette.py"""

import unittest

from palette_lib import (
    PaletteError,
    build_palette,
    histogram,
    indexed_to_truecolor,
    nearest_index,
    roundtrip_error,
    truecolor_to_indexed,
    truecolor_to_indexed_naive,
    validate_palette,
)

GRAY = (128, 128, 128)


def rgb_image(pixels):
    return bytes(c for px in pixels for c in px)


class TestValidation(unittest.TestCase):
    def test_empty_palette_rejected(self):
        with self.assertRaises(PaletteError) as cm:
            validate_palette([])
        self.assertIn("empty", str(cm.exception))

    def test_none_palette_rejected(self):
        with self.assertRaises(PaletteError):
            validate_palette(None)

    def test_missing_entry_reports_position(self):
        with self.assertRaises(PaletteError) as cm:
            validate_palette([(0, 0, 0), None, (255, 255, 255)])
        self.assertIn("palette entry 1", str(cm.exception))
        self.assertEqual(cm.exception.position, "palette entry 1")

    def test_wrong_length_entry_reports_position(self):
        with self.assertRaises(PaletteError) as cm:
            validate_palette([(0, 0, 0), (10, 20)])
        self.assertIn("palette entry 1", str(cm.exception))
        self.assertIn("3 channels", str(cm.exception))

    def test_channel_out_of_range(self):
        with self.assertRaises(PaletteError) as cm:
            validate_palette([(0, 0, 256)])
        self.assertIn("palette entry 0", str(cm.exception))

    def test_too_many_entries(self):
        with self.assertRaises(PaletteError):
            validate_palette([(i, i, i) for i in range(257)])

    def test_rgb_length_not_multiple_of_3(self):
        with self.assertRaises(PaletteError) as cm:
            truecolor_to_indexed(b"\x01\x02", [(0, 0, 0)])
        self.assertIn("byte 0", str(cm.exception))

    def test_index_out_of_range_reports_pixel(self):
        pal = [(0, 0, 0), (255, 255, 255)]
        with self.assertRaises(PaletteError) as cm:
            indexed_to_truecolor(bytes([0, 1, 7, 1]), pal)
        self.assertIn("index 7", str(cm.exception))
        self.assertIn("pixel 2", str(cm.exception))
        self.assertEqual(cm.exception.position, "pixel 2")

    def test_empty_indexed_image_ok(self):
        self.assertEqual(indexed_to_truecolor(b"", [(1, 2, 3)]), b"")

    def test_unknown_metric_rejected(self):
        with self.assertRaises(PaletteError):
            nearest_index((1, 2, 3), [(0, 0, 0)], metric="bogus")


class TestConversion(unittest.TestCase):
    def test_monochrome_exact_roundtrip(self):
        img = rgb_image([GRAY] * 100)
        idx = truecolor_to_indexed(img, [GRAY])
        self.assertEqual(idx, bytes(100))
        self.assertEqual(indexed_to_truecolor(idx, [GRAY]), img)
        stats = roundtrip_error(img, [GRAY])
        self.assertEqual(stats["max_error"], 0)
        self.assertEqual(stats["mean_error"], 0.0)

    def test_full_palette_256_roundtrip(self):
        pal = [(i, 255 - i, (i * 7) % 256) for i in range(256)]
        img = rgb_image(pal)  # image uses every palette entry
        idx = truecolor_to_indexed(img, pal)
        self.assertEqual(list(idx), list(range(256)))
        self.assertEqual(indexed_to_truecolor(idx, pal), img)
        stats = roundtrip_error(img, pal)
        self.assertEqual(stats["max_error"], 0)

    def test_nearest_selection_and_tie_break(self):
        pal = [(0, 0, 0), (255, 255, 255), (200, 0, 0)]
        self.assertEqual(nearest_index((250, 250, 250), pal), 1)
        self.assertEqual(nearest_index((190, 10, 10), pal), 2)
        # exact midpoint -> lowest index wins the tie
        self.assertEqual(nearest_index((127, 127, 127),
                                       [(0, 0, 0), (254, 254, 254)],
                                       metric="euclidean"), 0)

    def test_fast_and_naive_agree(self):
        pal = build_palette(rgb_image(
            [(x % 256, (x * 3) % 256, (x * 7) % 256) for x in range(500)]), 64)
        img = rgb_image([(x % 256, (x * 5) % 256, (x * 11) % 256)
                         for x in range(2000)])
        self.assertEqual(truecolor_to_indexed(img, pal),
                         truecolor_to_indexed_naive(img, pal))
        self.assertEqual(truecolor_to_indexed(img, pal, "euclidean"),
                         truecolor_to_indexed_naive(img, pal, "euclidean"))


class TestOverflow(unittest.TestCase):
    def _noisy_image(self, n):
        return rgb_image([((i * 37) % 256, (i * 91) % 256, (i * 53) % 256)
                          for i in range(n)])

    def test_overflow_median_cut_fits_capacity(self):
        img = self._noisy_image(1000)  # 1000 unique colors
        self.assertGreater(len(histogram(img)), 16)
        pal = build_palette(img, 16)
        self.assertLessEqual(len(pal), 16)
        idx = truecolor_to_indexed(img, pal)
        self.assertLess(max(idx), len(pal))
        stats = roundtrip_error(img, pal)
        self.assertGreaterEqual(stats["max_error"], stats["mean_error"])
        self.assertLess(stats["mean_error"], 64)  # quantization is sane

    def test_overflow_reject_raises(self):
        img = self._noisy_image(300)
        with self.assertRaises(PaletteError) as cm:
            build_palette(img, 16, strategy="reject")
        self.assertIn("exceeds palette capacity 16", str(cm.exception))

    def test_no_overflow_returns_exact_colors(self):
        img = rgb_image([(1, 2, 3), (4, 5, 6), (1, 2, 3)])
        pal = build_palette(img, 8)
        self.assertEqual(sorted(pal), [(1, 2, 3), (4, 5, 6)])

    def test_unknown_strategy_rejected(self):
        with self.assertRaises(PaletteError):
            build_palette(self._noisy_image(300), 4, strategy="modulo")

    def test_bad_capacity_rejected(self):
        for bad in (0, 257, -1, 1.5, "16"):
            with self.assertRaises(PaletteError):
                build_palette(b"\x00\x00\x00", bad)


class TestErrorStats(unittest.TestCase):
    def test_roundtrip_stats_fields(self):
        img = rgb_image([(10, 20, 30), (200, 210, 220), (10, 20, 30)])
        pal = [(0, 0, 0), (255, 255, 255)]
        stats = roundtrip_error(img, pal)
        self.assertEqual(stats["pixels"], 3)
        self.assertEqual(stats["unique_colors"], 2)
        self.assertEqual(stats["palette_size"], 2)
        # (10,20,30)x2 ->(0,0,0): errs 10,20,30; (200,210,220)->white: 55,45,35
        self.assertEqual(stats["max_error"], 55)
        self.assertAlmostEqual(stats["mean_error"],
                               (2 * (10 + 20 + 30) + (55 + 45 + 35)) / 9.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

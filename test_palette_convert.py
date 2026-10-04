"""palette_convert 自测: python3 test_palette_convert.py [-v]"""

import random
import unittest

from palette_convert import (
    PaletteError,
    error_stats,
    indexed_to_rgb,
    rgb_to_indexed,
    rgb_to_indexed_naive,
)


def gradient_palette():
    """6x6x6 颜色立方体 + 40 级灰度 = 256 色满色板。"""
    pal = [(r, g, b) for r in range(0, 256, 51)
           for g in range(0, 256, 51) for b in range(0, 256, 51)]
    pal += [(v, v, v) for v in range(4, 256, 6)][:40]
    assert len(pal) == 256
    return pal


class TestRoundTrip(unittest.TestCase):
    def test_single_color_image_exact(self):
        """单色图: 颜色在调色板中, 往返必须无损。"""
        palette = [(10, 20, 30), (200, 100, 50)]
        rgb = bytes([10, 20, 30] * 100)
        indexed = rgb_to_indexed(rgb, palette)
        self.assertEqual(indexed, bytes([0] * 100))
        self.assertEqual(indexed_to_rgb(indexed, palette), rgb)
        stats = error_stats(rgb, indexed_to_rgb(indexed, palette))
        self.assertEqual(stats["max_error"], 0)
        self.assertEqual(stats["mean_error"], 0.0)

    def test_full_palette_256(self):
        """满色板(256 色): 图像恰好用完 256 个下标, 往返无损。"""
        palette = gradient_palette()
        self.assertEqual(len(palette), 256)
        rgb = b"".join(bytes(c) for c in palette)  # 256 像素, 每色一次
        indexed = rgb_to_indexed(rgb, palette)
        self.assertEqual(sorted(set(indexed)), list(range(256)))
        self.assertEqual(indexed_to_rgb(indexed, palette), rgb)

    def test_roundtrip_with_quantization_error(self):
        """颜色不在调色板中: 往返有误差, 但误差有界且被统计。"""
        palette = [(0, 0, 0), (255, 255, 255)]
        rgb = bytes([130, 130, 130] * 10)
        back = indexed_to_rgb(rgb_to_indexed(rgb, palette), palette)
        stats = error_stats(rgb, back)
        self.assertEqual(stats["max_error"], 125)
        self.assertAlmostEqual(stats["mean_error"], 125.0)

    def test_nearest_color_selection(self):
        """选色依据: 应选距离最近者, 而非取模/截断。"""
        palette = [(0, 0, 0), (254, 0, 0), (0, 0, 254)]
        # (200,10,10) 明显离红色最近
        indexed = rgb_to_indexed(bytes([200, 10, 10]), palette)
        self.assertEqual(indexed, bytes([1]))
        # (3,3,3) 离黑色最近
        self.assertEqual(rgb_to_indexed(bytes([3, 3, 3]), palette), bytes([0]))

    def test_no_modulo_mapping(self):
        """回归: 索引数不得对调色板长度取模导致色带跳变。"""
        palette = [(i, i, i) for i in range(0, 256, 32)]  # 8 色灰阶
        rgb = bytes([250, 250, 250])
        index = rgb_to_indexed(rgb, palette)[0]
        self.assertEqual(index, 7)  # 最近色 (224,224,224), 而不是 250 % 8 = 2

    def test_metrics_agree_on_exact_match(self):
        for metric in ("euclidean", "weighted", "redmean"):
            palette = [(1, 2, 3), (250, 250, 250)]
            self.assertEqual(rgb_to_indexed(bytes([1, 2, 3]), palette, metric=metric),
                             bytes([0]))

    def test_kdtree_matches_naive(self):
        """KD 树+缓存与朴素逐像素扫描: 选中色距离必须一致(平局允许不同下标)。"""
        from palette_convert import _distance
        rng = random.Random(42)
        palette = [(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                   for _ in range(256)]
        rgb = bytes(rng.randrange(256) for _ in range(3000 * 3))
        for metric in ("euclidean", "weighted", "redmean"):
            dist, _ = _distance(metric)
            fast = rgb_to_indexed(rgb, palette, metric=metric)
            ref = rgb_to_indexed_naive(rgb, palette, metric=metric)
            self.assertEqual(len(fast), len(ref))
            for k, (a, b) in enumerate(zip(fast, ref)):
                color = (rgb[3 * k], rgb[3 * k + 1], rgb[3 * k + 2])
                self.assertEqual(dist(color, palette[a]), dist(color, palette[b]),
                                 msg=f"metric={metric} pixel={k}")
            # 注: 平局时两者可能选中不同的等距异色, 因此逐通道误差统计
            # (如 mean_error) 允许不同; 上面的逐像素距离相等才是正确性判据。


class TestOverflow(unittest.TestCase):
    def test_reject_over_capacity(self):
        """reject: 不同颜色数 > capacity 时拒绝, 并指出像素位置。"""
        palette = gradient_palette()
        rgb = b"".join(bytes((i & 255, (i >> 8) & 255, 0)) for i in range(300))  # 300 种颜色
        with self.assertRaises(PaletteError) as ctx:
            rgb_to_indexed(rgb, palette, overflow="reject")
        self.assertIn("pixel 256", str(ctx.exception))

    def test_reject_within_capacity_ok(self):
        palette = gradient_palette()
        rgb = b"".join(bytes((i, i, i)) for i in range(200))  # 200 种颜色
        indexed = rgb_to_indexed(rgb, palette, overflow="reject")
        self.assertEqual(len(indexed), 200)

    def test_quantize_over_capacity_ok(self):
        """quantize(默认): 超容量不报错, 量化到最近色。"""
        palette = gradient_palette()
        rgb = b"".join(bytes((i & 255, (i >> 8) & 255, 0)) for i in range(300))
        indexed = rgb_to_indexed(rgb, palette, overflow="quantize")
        self.assertEqual(len(indexed), 300)

    def test_custom_capacity(self):
        palette = gradient_palette()
        rgb = b"".join(bytes((i, 0, 0)) for i in range(10))
        with self.assertRaises(PaletteError):
            rgb_to_indexed(rgb, palette, overflow="reject", capacity=5)

    def test_palette_exceeds_capacity(self):
        palette = [((i & 255), (i >> 8) & 255, 7) for i in range(300)]
        with self.assertRaises(PaletteError) as ctx:
            rgb_to_indexed(bytes([1, 2, 3]), palette)
        self.assertIn("exceeds capacity", str(ctx.exception))


class TestValidation(unittest.TestCase):
    def test_empty_palette(self):
        """调色板为空: 非空图像必须拒绝。"""
        with self.assertRaises(PaletteError) as ctx:
            rgb_to_indexed(bytes([1, 2, 3]), [])
        self.assertIn("empty", str(ctx.exception))
        with self.assertRaises(PaletteError):
            indexed_to_rgb(bytes([0]), [])
        # 空图像 + 空调色板: 合法, 返回空
        self.assertEqual(rgb_to_indexed(b"", []), b"")
        self.assertEqual(indexed_to_rgb(b"", []), b"")

    def test_palette_entry_wrong_length(self):
        """条目长度不符: 指出条目位置。"""
        palette = [(0, 0, 0), (1, 2), (3, 3, 3)]
        with self.assertRaises(PaletteError) as ctx:
            rgb_to_indexed(bytes([0, 0, 0]), palette)
        self.assertIn("entry 1", str(ctx.exception))
        self.assertIn("length 2", str(ctx.exception))

    def test_palette_entry_missing(self):
        palette = [(0, 0, 0), None]
        with self.assertRaises(PaletteError) as ctx:
            indexed_to_rgb(bytes([0]), palette)
        self.assertIn("entry 1", str(ctx.exception))

    def test_palette_component_out_of_range(self):
        palette = [(0, 0, 0), (256, 0, 0)]
        with self.assertRaises(PaletteError) as ctx:
            rgb_to_indexed(bytes([0, 0, 0]), palette)
        self.assertIn("entry 1", str(ctx.exception))
        self.assertIn("0..255", str(ctx.exception))

    def test_palette_component_not_int(self):
        palette = [(0, 0, 0), ("red", 0, 0)]
        with self.assertRaises(PaletteError) as ctx:
            rgb_to_indexed(bytes([0, 0, 0]), palette)
        self.assertIn("entry 1", str(ctx.exception))

    def test_index_out_of_range(self):
        """索引越界: 指出像素位置与越界值。"""
        palette = [(0, 0, 0), (255, 255, 255)]
        data = bytes([0, 1, 0, 5])
        with self.assertRaises(PaletteError) as ctx:
            indexed_to_rgb(data, palette)
        msg = str(ctx.exception)
        self.assertIn("pixel 3", msg)
        self.assertIn("index 5", msg)

    def test_rgb_length_not_multiple_of_3(self):
        with self.assertRaises(PaletteError) as ctx:
            rgb_to_indexed(bytes([1, 2, 3, 4]), [(0, 0, 0)])
        self.assertIn("multiple of 3", str(ctx.exception))

    def test_error_stats_length_mismatch(self):
        with self.assertRaises(PaletteError):
            error_stats(bytes(6), bytes(3))

    def test_error_stats_values(self):
        a = bytes([0, 0, 0, 100, 100, 100])
        b = bytes([10, 0, 0, 90, 100, 110])
        stats = error_stats(a, b)
        self.assertEqual(stats["pixels"], 2)
        self.assertEqual(stats["max_error"], 10)
        self.assertAlmostEqual(stats["mean_error"], 30 / 6)
        self.assertAlmostEqual(stats["rms_error"], (300 / 6) ** 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

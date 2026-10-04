"""vxl 自测: 布局解析、对拍、越界、长度不匹配、无损往返、边界情形。"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vxl import (
    AttributeDataError,
    FormatError,
    IndexBoundsError,
    Mesh,
    build,
)
from vxl.format import ATTR_SIZE, HEADER_SIZE
from vxl.writer import assemble_file

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")

ATTR_SPECS = [
    ("position", 3, "float32"),
    ("normal", 3, "float32"),
    ("uv", 2, "float32"),
]


def load(name):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


class CrossLayoutTest(unittest.TestCase):
    """交错与分行两种布局读出的顶点必须逐分量一致。"""

    def test_interleaved_matches_planar(self):
        inter = Mesh(load("crosscheck_interleaved.vxb"))
        planar = Mesh(load("crosscheck_planar.vxb"))
        self.assertEqual(inter.vertex_count, planar.vertex_count)
        self.assertEqual(list(inter.iter_indices()), list(planar.iter_indices()))
        for name in ("position", "normal", "uv"):
            acc_i = inter.attribute(name)
            acc_p = planar.attribute(name)
            self.assertEqual(len(acc_i), len(acc_p))
            for vi, vp in zip(acc_i, acc_p):
                self.assertEqual(vi, vp)  # 同一 float 位模式, 必须精确相等

    def test_mixed_layout_matches_pure_layouts(self):
        mixed = Mesh(load("mixed.vxb"))
        inter = Mesh(load("crosscheck_interleaved.vxb"))
        layouts = {a.name: a.layout_name for a in mixed.attributes}
        self.assertEqual(layouts["position"], "interleaved")
        self.assertEqual(layouts["normal"], "interleaved")
        self.assertEqual(layouts["uv"], "planar")
        for name in ("position", "normal", "uv"):
            for vm, vi in zip(mixed.attribute(name), inter.attribute(name)):
                self.assertEqual(vm, vi)

    def test_stride_offset_math(self):
        mesh = Mesh(load("crosscheck_interleaved.vxb"))
        pos = mesh.attribute("position")
        uv = mesh.attribute("uv")
        # stride=32: 第 i 个顶点的 position 在 i*32, uv 在 i*32+24
        data = mesh._data
        expect_px = struct.unpack_from("<f", data, 5 * 32)[0]
        expect_u = struct.unpack_from("<f", data, 5 * 32 + 24)[0]
        self.assertEqual(pos[5][0], expect_px)
        self.assertEqual(uv[5][0], expect_u)


class RoundTripTest(unittest.TestCase):
    """写出后重新读入必须逐字节与原始数据一致。"""

    def test_to_bytes_is_lossless(self):
        for name in ("roundtrip.vxb", "crosscheck_planar.vxb", "mixed.vxb",
                     "single_vertex.vxb", "lines.vxb", "quads.vxb"):
            raw = load(name)
            mesh = Mesh(raw)
            rebuilt = mesh.to_bytes()
            self.assertEqual(rebuilt, raw, "round trip mismatch for %s" % name)
            # 写出的字节再读一次, 仍然一致
            mesh2 = Mesh(rebuilt)
            self.assertEqual(mesh2.to_bytes(), raw)


class IndexValidationTest(unittest.TestCase):
    """引用不存在的顶点要拒绝并指出图元与索引值。"""

    def test_oob_index_rejected_with_primitive_and_value(self):
        with self.assertRaises(IndexBoundsError) as ctx:
            Mesh(load("oob_index.vxb"))
        msg = str(ctx.exception)
        self.assertIn("primitive #1", msg)
        self.assertIn("triangles", msg)
        self.assertIn("9999", msg)

    def test_all_oob_indices_reported(self):
        mesh = Mesh(load("oob_index.vxb"), strict_indices=False)
        errors = mesh.find_index_errors()
        self.assertEqual(len(errors), 2)
        self.assertEqual((errors[0].prim_index, errors[0].corner,
                          errors[0].value), (1, 2, 9999))
        self.assertEqual((errors[1].prim_index, errors[1].corner,
                          errors[1].value), (2, 1, 12345))

    def test_valid_indices_never_read_neighbor_data(self):
        # 顶点数恰好等于最大索引+1 时必须通过, 不允许误报
        raw = build(
            [{"position": (1.0, 2.0, 3.0), "normal": (0.0, 1.0, 0.0),
              "uv": (0.5, 0.5)}] * 3,
            ATTR_SPECS, layout="interleaved",
            primitive="triangles", indices=[2, 1, 0])
        mesh = Mesh(raw)
        self.assertEqual(list(mesh.primitives()), [(2, 1, 0)])


class AttributeLengthTest(unittest.TestCase):
    """属性声明与数据长度不匹配要报告并指出位置。"""

    def test_short_data_section_reports_position(self):
        with self.assertRaises(AttributeDataError) as ctx:
            Mesh(load("bad_attr_length.vxb"))
        exc = ctx.exception
        self.assertEqual(exc.name, "position")
        self.assertEqual(exc.vertex, 2)
        self.assertEqual(exc.byte_offset, 24)
        self.assertIn("vertex 2", str(exc))
        self.assertIn("byte offset 24", str(exc))

    def test_stride_smaller_than_components(self):
        # 声明 3 个 float(12 字节) 但步长只有 8(两个 float)
        data = struct.pack("<6f", *range(6))
        raw = assemble_file(
            vertex_count=2,
            attributes=[("position", "interleaved", 3, "float32", 8, 0)],
            primitive="lines", data_section=data, indices=[0, 1])
        with self.assertRaises(FormatError) as ctx:
            Mesh(raw)
        self.assertIn("stride 8", str(ctx.exception))
        self.assertIn("12", str(ctx.exception))


class EdgeCaseTest(unittest.TestCase):
    """单顶点、非三角形图元、坏文件头等边界情形。"""

    def test_single_vertex(self):
        mesh = Mesh(load("single_vertex.vxb"))
        self.assertEqual(mesh.vertex_count, 1)
        self.assertEqual(mesh.primitive_name, "points")
        self.assertEqual(len(mesh.attribute("position")), 1)
        self.assertEqual(len(mesh.attribute("position")[0]), 3)
        self.assertEqual(list(mesh.primitives()), [(0,)])
        with self.assertRaises(IndexError):
            mesh.attribute("position")[1]

    def test_lines_primitive(self):
        mesh = Mesh(load("lines.vxb"))
        self.assertEqual(mesh.primitive_name, "lines")
        self.assertEqual(list(mesh.primitives()), [(0, 1), (1, 2), (2, 3)])

    def test_quads_primitive(self):
        mesh = Mesh(load("quads.vxb"))
        self.assertEqual(mesh.primitive_name, "quads")
        self.assertEqual(list(mesh.primitives()), [(0, 1, 2, 3)])

    def test_dangling_indices_rejected(self):
        raw = build(
            [{"position": (0.0, 0.0, 0.0), "normal": (0.0, 0.0, 1.0),
              "uv": (0.0, 0.0)}] * 4,
            ATTR_SPECS, layout="planar",
            primitive="triangles", indices=[0, 1, 2, 3])
        with self.assertRaises(FormatError) as ctx:
            Mesh(raw)
        self.assertIn("dangling", str(ctx.exception))

    def test_bad_magic(self):
        raw = bytearray(load("single_vertex.vxb"))
        raw[0:4] = b"XXXX"
        with self.assertRaises(FormatError) as ctx:
            Mesh(bytes(raw))
        self.assertIn("bad magic", str(ctx.exception))

    def test_truncated_header(self):
        with self.assertRaises(FormatError) as ctx:
            Mesh(b"VX")
        self.assertIn("truncated header", str(ctx.exception))

    def test_truncated_index_section(self):
        # 截断长度超过数据区(32 字节), 缺口才会落到索引区
        raw = load("single_vertex.vxb")[:-34]
        with self.assertRaises(FormatError) as ctx:
            Mesh(raw)
        self.assertIn("truncated index section", str(ctx.exception))

    def test_truncated_data_section_reports_attribute(self):
        # 截断 2 字节: 缺口落在数据区, 报属性数据越界并指出位置
        raw = load("single_vertex.vxb")[:-2]
        with self.assertRaises(AttributeDataError) as ctx:
            Mesh(raw)
        self.assertEqual(ctx.exception.name, "uv")
        self.assertEqual(ctx.exception.vertex, 0)

    def test_unknown_attribute(self):
        mesh = Mesh(load("single_vertex.vxb"))
        with self.assertRaises(KeyError):
            mesh.attribute("tangent")

    def test_accessor_bounds(self):
        mesh = Mesh(load("single_vertex.vxb"))
        with self.assertRaises(IndexError):
            mesh.attribute("uv")[-1]
        with self.assertRaises(IndexError):
            mesh.index(7)

    def test_uint16_attribute(self):
        # 非 float 属性: uint16 分行布局
        vertices = [{"bone": (1, 2, 3, 4)} for _ in range(3)]
        raw = build(vertices, [("bone", 4, "uint16")], layout="planar",
                    primitive="triangles", indices=[0, 1, 2])
        mesh = Mesh(raw)
        self.assertEqual(mesh.attribute("bone")[2], (1, 2, 3, 4))
        self.assertEqual(mesh.to_bytes(), raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)

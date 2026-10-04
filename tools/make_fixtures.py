"""生成 fixtures/ 下的对拍数据、无损往返数据与各类边界样本。"""

import os
import random
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vxl.format import (
    ATTR_FMT,
    HEADER_FMT,
    LAYOUT_INTERLEAVED,
    LAYOUT_PLANAR,
    MAGIC,
)
from vxl.writer import assemble_file, build

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")

ATTR_SPECS = [
    ("position", 3, "float32"),
    ("normal", 3, "float32"),
    ("uv", 2, "float32"),
]


def make_vertices(n, seed=1234):
    rng = random.Random(seed)
    vertices = []
    for _ in range(n):
        vertices.append({
            "position": tuple(rng.uniform(-100.0, 100.0) for _ in range(3)),
            "normal": tuple(rng.uniform(-1.0, 1.0) for _ in range(3)),
            "uv": (rng.uniform(0.0, 1.0), rng.uniform(0.0, 1.0)),
        })
    return vertices


def make_indices(n, seed=99):
    rng = random.Random(seed)
    indices = []
    while len(indices) + 3 <= (n // 3) * 3:
        a, b, c = rng.randrange(n), rng.randrange(n), rng.randrange(n)
        indices += [a, b, c]
    return indices


def write(name, data):
    path = os.path.join(FIXTURES, name)
    with open(path, "wb") as fh:
        fh.write(data)
    print("wrote %s (%d bytes)" % (path, len(data)))


def main():
    os.makedirs(FIXTURES, exist_ok=True)
    vertices = make_vertices(1024)
    indices = make_indices(1024)

    # 1) 对拍数据: 同一批顶点, 交错与分行各一份
    write("crosscheck_interleaved.vxb", build(
        vertices, ATTR_SPECS, layout="interleaved",
        primitive="triangles", indices=indices))
    write("crosscheck_planar.vxb", build(
        vertices, ATTR_SPECS, layout="planar",
        primitive="triangles", indices=indices))

    # 2) 混用布局: position+normal 交错, uv 分行
    write("mixed.vxb", build(
        vertices, ATTR_SPECS, layout="mixed",
        groups=[("interleaved", ATTR_SPECS[:2]), ("planar", ATTR_SPECS[2:])],
        primitive="triangles", indices=indices))

    # 3) 无损往返样本: 交错 + 尾部填充对齐(stride 含 padding)
    write("roundtrip.vxb", build(
        vertices, ATTR_SPECS, layout="interleaved",
        primitive="triangles", indices=indices))

    # 4) 越界索引样本: 4 个顶点, 索引引用 9999
    small = make_vertices(4, seed=7)
    write("oob_index.vxb", build(
        small, ATTR_SPECS, layout="interleaved",
        primitive="triangles",
        indices=[0, 1, 2, 1, 2, 9999, 3, 12345, 0]))

    # 5) 属性声明与数据长度不匹配: position 声明 3 分量, 数据每顶点只有 2 个 float
    n = 3
    bad_data = struct.pack("<%df" % (n * 2),
                           0.0, 0.0, 1.0, 0.0, 0.0, 1.0)
    attrs = [("position", LAYOUT_PLANAR, 3, 0, 12, 0)]
    write("bad_attr_length.vxb", assemble_file(
        vertex_count=n, attributes=attrs, primitive="triangles",
        data_section=bad_data, indices=[0, 1, 2]))

    # 6) 单顶点 + points 图元
    one = make_vertices(1, seed=5)
    write("single_vertex.vxb", build(
        one, ATTR_SPECS, layout="interleaved",
        primitive="points", indices=[0]))

    # 7) 非三角形图元: lines 与 quads
    four = make_vertices(4, seed=11)
    write("lines.vxb", build(
        four, ATTR_SPECS, layout="planar",
        primitive="lines", indices=[0, 1, 1, 2, 2, 3]))
    write("quads.vxb", build(
        four, ATTR_SPECS, layout="interleaved",
        primitive="quads", indices=[0, 1, 2, 3]))


if __name__ == "__main__":
    main()

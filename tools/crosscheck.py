"""对拍: 读入交错/分行(及混用)两份数据, 逐顶点逐分量比较。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vxl.reader import Mesh

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def compare(path_a, path_b, label):
    a = Mesh(open(path_a, "rb").read())
    b = Mesh(open(path_b, "rb").read())
    assert a.vertex_count == b.vertex_count, "vertex count differs"
    names = [info.name for info in a.attributes]
    checked = 0
    for name in names:
        acc_a, acc_b = a.attribute(name), b.attribute(name)
        for i, (va, vb) in enumerate(zip(acc_a, acc_b)):
            assert va == vb, "%s mismatch at vertex %d attr %s: %r != %r" % (
                label, i, name, va, vb)
            for x, y in zip(va, vb):
                assert x == y
            checked += len(va)
    ia = list(a.iter_indices())
    ib = list(b.iter_indices())
    assert ia == ib, "index buffers differ"
    print("%s: %d vertices, %d components compared component-wise, "
          "%d indices: EXACT MATCH"
          % (label, a.vertex_count, checked, len(ia)))


def main():
    base = FIXTURES
    compare(os.path.join(base, "crosscheck_interleaved.vxb"),
            os.path.join(base, "crosscheck_planar.vxb"),
            "interleaved vs planar")
    compare(os.path.join(base, "crosscheck_interleaved.vxb"),
            os.path.join(base, "mixed.vxb"),
            "interleaved vs mixed")
    # 顺带验证无损往返
    rt = os.path.join(base, "roundtrip.vxb")
    raw = open(rt, "rb").read()
    assert Mesh(raw).to_bytes() == raw
    print("roundtrip.vxb: to_bytes() == original bytes: BYTE-IDENTICAL")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Self-tests for the VXL1 vertex layout library (stdlib only).

Run: python3 selftest.py
Exit code 0 = all tests passed. Also writes data/oob_report.txt.
"""

import os
import struct
import sys
import traceback

from vertex_layout import (
    Attribute, Model, Primitive, parse,
    LAYOUT_INTERLEAVED, LAYOUT_PLANAR,
    PRIM_POINTS, PRIM_LINES, PRIM_TRIANGLES, PRIM_QUADS, PRIM_NAMES,
    VXLError, TruncatedDataError, LayoutMismatchError, IndexOutOfBoundsError,
)
from make_fixtures import mesh_data

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

FAILURES = []


def check(name, fn):
    try:
        fn()
    except Exception:
        FAILURES.append(name)
        print("FAIL %s" % name)
        traceback.print_exc()
    else:
        print("PASS %s" % name)


def load(name):
    with open(os.path.join(DATA, name), "rb") as f:
        return f.read()


def f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def f32row(row):
    return tuple(f32(v) for v in row)


ATTRS = ("position", "normal", "uv")


def test_cross_layout_equal():
    """Interleaved, planar and mixed files must read back identical values."""
    mi = parse(load("interleaved.vxl"))
    mp = parse(load("planar.vxl"))
    mm = parse(load("mixed.vxl"))
    pos, nrm, uv = mesh_data(12)
    expected = {"position": pos, "normal": nrm, "uv": uv}
    for i in range(12):
        for name in ATTRS:
            want = f32row(expected[name][i])
            vi = mi.vertex(i, name)
            vp = mp.vertex(i, name)
            vm = mm.vertex(i, name)
            assert vi == vp == vm == want, (
                "vertex %d attr %s: interleaved=%r planar=%r mixed=%r want=%r"
                % (i, name, vi, vp, vm, want))


def test_single_vertex():
    m = parse(load("single.vxl"))
    assert m.vertex_count == 1
    pos, nrm, uv = mesh_data(1)
    assert m.vertex(0, "position") == f32row(pos[0])
    assert m.vertex(0, "uv") == f32row(uv[0])
    assert m.primitives[0].ptype == PRIM_POINTS
    assert list(m.primitives[0].indices) == [0]
    # vertex 1 does not exist: must be rejected, not read from adjacent data
    try:
        m.vertex(1, "position")
    except IndexOutOfBoundsError:
        pass
    else:
        raise AssertionError("vertex(1) should have been rejected")


def test_mixed_layout_flag():
    m = parse(load("mixed.vxl"))
    assert m.attribute("position").layout == LAYOUT_INTERLEAVED
    assert m.attribute("normal").layout == LAYOUT_INTERLEAVED
    assert m.attribute("uv").layout == LAYOUT_PLANAR


def test_oob_index_rejected():
    raw = load("oob.vxl")
    report_lines = [
        "VXL1 out-of-bounds index report",
        "file: data/oob.vxl",
        "vertex_count: 12",
        "offending indices in file: 12 (index[2]) and 999 (index[5])",
        "",
    ]
    try:
        parse(raw)
    except IndexOutOfBoundsError as e:
        msg = str(e)
        report_lines.append("parser verdict: REJECTED")
        report_lines.append("error: " + msg)
        report_lines.append(
            "note: the index was rejected during validation, before any "
            "vertex data was dereferenced; no neighbouring data was read.")
        assert "primitive #0" in msg and "triangles" in msg, msg
        assert "index[2] = 12" in msg, msg
        assert "vertex_count=12" in msg, msg
    else:
        raise AssertionError("oob.vxl should have been rejected")
    with open(os.path.join(DATA, "oob_report.txt"), "w") as f:
        f.write("\n".join(report_lines) + "\n")


def test_attr_length_mismatch():
    try:
        parse(load("badlen.vxl"))
    except LayoutMismatchError as e:
        msg = str(e)
        assert "'normal'" in msg, msg
        assert "declared 3 components" in msg, msg
        assert "only 8 of 12 bytes" in msg, msg
        assert "file offset" in msg, msg
    else:
        raise AssertionError("badlen.vxl should have been rejected")


def test_non_triangle_prims():
    m = parse(load("nontri.vxl"))
    types = [p.ptype for p in m.primitives]
    assert types == [PRIM_POINTS, PRIM_LINES, PRIM_QUADS, PRIM_TRIANGLES], types
    assert m.primitives[0].groups() == [(0,), (3,), (6,), (9,)]
    assert m.primitives[1].groups() == [(0, 1), (2, 3), (4, 5)]
    assert m.primitives[2].groups() == [(0, 1, 2, 3), (4, 5, 6, 7)]
    assert m.primitives[3].groups() == [(8, 9, 10), (9, 10, 11)]
    # a lines primitive with an odd index count must be rejected
    bad = Model(12, m.attributes,
                [Primitive(PRIM_LINES, [0, 1, 2])], bytes(m.data_region))
    try:
        parse(bad.serialize())
    except VXLError as e:
        assert "not a multiple of 2" in str(e)
    else:
        raise AssertionError("odd line index count should be rejected")


def test_roundtrip_lossless():
    for name in ("interleaved.vxl", "planar.vxl", "mixed.vxl",
                 "single.vxl", "nontri.vxl", "roundtrip.vxl"):
        raw = load(name)
        m1 = parse(raw)
        out = m1.serialize()
        assert out == raw, "%s: serialize(parse(raw)) != raw" % name
        m2 = parse(out)
        assert m2.serialize() == raw, "%s: second round-trip diverged" % name
        assert m2.vertex_count == m1.vertex_count
        for i in range(m1.vertex_count):
            for a in m1.attributes:
                assert m2.vertex(i, a.name) == m1.vertex(i, a.name)
        for p1, p2 in zip(m1.primitives, m2.primitives):
            assert p1.ptype == p2.ptype
            assert list(p1.indices) == list(p2.indices)


def test_stride_validation():
    # stride 8 < 3 components * 4 bytes: vertices would overlap
    a = Attribute("position", 3, LAYOUT_INTERLEAVED, 8, 0)
    bad = Model(2, [a], [], b"\x00" * 24)
    try:
        parse(bad.serialize())
    except LayoutMismatchError as e:
        assert "stride 8 too small" in str(e)
    else:
        raise AssertionError("overlapping stride should be rejected")


def test_truncation_rejected():
    raw = load("roundtrip.vxl")
    for cut in (0, 5, 20, len(raw) // 2, len(raw) - 1):
        try:
            parse(raw[:cut])
        except VXLError:
            pass
        else:
            raise AssertionError("truncated file (%d bytes) accepted" % cut)
    # trailing garbage must also be rejected
    try:
        parse(raw + b"\x00")
    except VXLError as e:
        assert "trailing" in str(e)
    else:
        raise AssertionError("trailing bytes accepted")


def test_iter_attr_matches_vertex():
    m = parse(load("mixed.vxl"))
    for name in ATTRS:
        seq = list(m.iter_attr(name))
        assert len(seq) == 12
        for i, v in enumerate(seq):
            assert v == m.vertex(i, name)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        check(t.__name__, t)
    print("-" * 60)
    if FAILURES:
        print("%d/%d tests FAILED: %s" % (len(FAILURES), len(tests), FAILURES))
        return 1
    print("all %d tests passed" % len(tests))
    return 0


if __name__ == "__main__":
    sys.exit(main())

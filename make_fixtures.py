"""Generate the VXL1 fixture data under data/ (stdlib only).

Run: python3 make_fixtures.py
"""

import os
import struct
from array import array

from vertex_layout import (
    Attribute, Model, Primitive,
    LAYOUT_INTERLEAVED, LAYOUT_PLANAR,
    PRIM_POINTS, PRIM_LINES, PRIM_TRIANGLES, PRIM_QUADS,
)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

N = 12


def mesh_data(n):
    """Deterministic (non round-float) per-attribute data for n vertices."""
    pos = [(i * 1.5, i * 0.25 - 3.0, 2.0 - i) for i in range(n)]
    nrm = [((i % 3) - 1.0, 0.5, 0.25 * (i % 2)) for i in range(n)]
    uv = [(i * 0.125, 1.0 - i * 0.0625) for i in range(n)]
    return pos, nrm, uv


def fblock(rows):
    a = array("f")
    for row in rows:
        a.extend(row)
    return a.tobytes()


def write(name, model, raw=None):
    if raw is None:
        raw = model.serialize()
    path = os.path.join(DATA, name)
    with open(path, "wb") as f:
        f.write(raw)
    print("wrote %s (%d bytes)" % (path, len(raw)))


def interl_attrs(stride, offsets):
    return [
        Attribute("position", 3, LAYOUT_INTERLEAVED, stride, offsets[0]),
        Attribute("normal", 3, LAYOUT_INTERLEAVED, stride, offsets[1]),
        Attribute("uv", 2, LAYOUT_INTERLEAVED, stride, offsets[2]),
    ]


def planar_attrs(n):
    return [
        Attribute("position", 3, LAYOUT_PLANAR, 12, 0),
        Attribute("normal", 3, LAYOUT_PLANAR, 12, 12 * n),
        Attribute("uv", 2, LAYOUT_PLANAR, 8, 24 * n),
    ]


def main():
    os.makedirs(DATA, exist_ok=True)
    pos, nrm, uv = mesh_data(N)
    tris = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    tri_prim = Primitive(PRIM_TRIANGLES, array("I", tris))

    # --- interleaved.vxl: position/normal/uv interleaved, stride 32 ---
    rec8 = struct.Struct("<8f")
    interl_blob = b"".join(rec8.pack(*(pos[i] + nrm[i] + uv[i])) for i in range(N))
    write("interleaved.vxl", Model(
        N, interl_attrs(32, (0, 12, 24)), [tri_prim], interl_blob))

    # --- planar.vxl: same logical mesh, one block per attribute ---
    planar_blob = fblock(pos) + fblock(nrm) + fblock(uv)
    write("planar.vxl", Model(
        N, planar_attrs(N), [Primitive(PRIM_TRIANGLES, array("I", tris))], planar_blob))

    # --- mixed.vxl: position+normal interleaved, uv planar ---
    rec6 = struct.Struct("<6f")
    interl_pn = b"".join(rec6.pack(*(pos[i] + nrm[i])) for i in range(N))
    mixed_blob = interl_pn + fblock(uv)
    mixed_attrs = [
        Attribute("position", 3, LAYOUT_INTERLEAVED, 24, 0),
        Attribute("normal", 3, LAYOUT_INTERLEAVED, 24, 12),
        Attribute("uv", 2, LAYOUT_PLANAR, 8, 24 * N),
    ]
    write("mixed.vxl", Model(
        N, mixed_attrs, [Primitive(PRIM_TRIANGLES, array("I", tris))], mixed_blob))

    # --- single.vxl: one vertex, planar position, a single point ---
    sp, sn, su = mesh_data(1)
    write("single.vxl", Model(
        1,
        [Attribute("position", 3, LAYOUT_PLANAR, 12, 0),
         Attribute("normal", 3, LAYOUT_PLANAR, 12, 12),
         Attribute("uv", 2, LAYOUT_PLANAR, 8, 24)],
        [Primitive(PRIM_POINTS, array("I", [0]))],
        fblock(sp) + fblock(sn) + fblock(su)))

    # --- nontri.vxl: points, lines, quads and triangles together ---
    nontri_prims = [
        Primitive(PRIM_POINTS, array("I", [0, 3, 6, 9])),
        Primitive(PRIM_LINES, array("I", [0, 1, 2, 3, 4, 5])),
        Primitive(PRIM_QUADS, array("I", [0, 1, 2, 3, 4, 5, 6, 7])),
        Primitive(PRIM_TRIANGLES, array("I", [8, 9, 10, 9, 10, 11])),
    ]
    write("nontri.vxl", Model(
        N, planar_attrs(N), nontri_prims, planar_blob))

    # --- oob.vxl: triangles reference vertex 12 and 999 (only 12 exist) ---
    oob_indices = [0, 1, 12, 3, 4, 999]
    write("oob.vxl", Model(
        N, planar_attrs(N),
        [Primitive(PRIM_TRIANGLES, array("I", oob_indices))], planar_blob))

    # --- badlen.vxl: normal declares 3 components but only 2 floats given ---
    bad_blob = fblock(pos[:1]) + fblock([nrm[0][:2]])  # 12 bytes position + 8 bytes normal
    bad_attrs = [
        Attribute("position", 3, LAYOUT_PLANAR, 12, 0),
        Attribute("normal", 3, LAYOUT_PLANAR, 12, 12),
    ]
    write("badlen.vxl", Model(1, bad_attrs, [], bad_blob))

    # --- roundtrip.vxl: mixed layouts, all primitive kinds, 7 vertices ---
    rpos, rnrm, ruv = mesh_data(7)
    r_pn = b"".join(rec6.pack(*(rpos[i] + rnrm[i])) for i in range(7))
    r_blob = r_pn + fblock(ruv)
    r_attrs = [
        Attribute("position", 3, LAYOUT_INTERLEAVED, 24, 0),
        Attribute("normal", 3, LAYOUT_INTERLEAVED, 24, 12),
        Attribute("uv", 2, LAYOUT_PLANAR, 8, 24 * 7),
    ]
    r_prims = [
        Primitive(PRIM_POINTS, array("I", [6])),
        Primitive(PRIM_LINES, array("I", [0, 1, 2, 3])),
        Primitive(PRIM_TRIANGLES, array("I", [0, 1, 2, 3, 4, 5])),
        Primitive(PRIM_QUADS, array("I", [0, 1, 2, 3, 4, 5, 6, 0])),
    ]
    write("roundtrip.vxl", Model(7, r_attrs, r_prims, r_blob))


if __name__ == "__main__":
    main()

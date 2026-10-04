"""VXL1 vertex attribute layout parsing library (Python 3, stdlib only).

Supports two vertex attribute storage layouts in one file:

* interleaved -- attributes packed per-vertex, located by stride + offset
* planar      -- each attribute stored as its own contiguous block

Both layouts are read through the same accessor and must yield identical
per-component values for the same logical mesh.

VXL1 binary layout (little-endian)::

    u8[4]  magic "VXL1"
    u32    vertex_count
    u32    attr_count
    u64    data_end            # file offset where the attribute data region
                               # ends and the primitive section begins
    repeat attr_count:
        u8     name_len, then name bytes (utf-8)
        u8     components      # 1..4 float32 components
        u8     layout          # 0 = interleaved, 1 = planar
        u32    stride          # bytes between vertices (interleaved; 0 if planar)
        u32    offset          # bytes from data region start to vertex 0 comp 0
    attribute data region: [data_start, data_end)
    u32    prim_count
    repeat prim_count:
        u8     prim_type       # 0=points 1=lines 2=triangles 3=quads
        u32    index_count
        u32    indices[index_count]

All indices are validated against vertex_count at parse time; an
out-of-bounds reference is rejected with the primitive id and the
offending index value, before any vertex data is dereferenced.
"""

from __future__ import annotations

import struct
import sys
from array import array
from dataclasses import dataclass, field

MAGIC = b"VXL1"
FLOAT_SIZE = 4

LAYOUT_INTERLEAVED = 0
LAYOUT_PLANAR = 1
LAYOUT_NAMES = {LAYOUT_INTERLEAVED: "interleaved", LAYOUT_PLANAR: "planar"}

PRIM_POINTS = 0
PRIM_LINES = 1
PRIM_TRIANGLES = 2
PRIM_QUADS = 3
PRIM_NAMES = {0: "points", 1: "lines", 2: "triangles", 3: "quads"}
PRIM_VERTS = {0: 1, 1: 2, 2: 3, 3: 4}

HEADER = struct.Struct("<4sIIQ")   # magic, vertex_count, attr_count, data_end
ATTR_FIXED = struct.Struct("<BBII")  # components, layout, stride, offset
PRIM_HEADER = struct.Struct("<BI")   # prim_type, index_count
U32 = struct.Struct("<I")


class VXLError(Exception):
    """Base class for all VXL1 parsing/validation errors."""


class TruncatedDataError(VXLError):
    """File ends before a declared structure is complete."""


class LayoutMismatchError(VXLError):
    """Attribute declaration does not match the available data length."""


class IndexOutOfBoundsError(VXLError):
    """A primitive references a vertex index that does not exist."""


def _u32_typecode() -> str:
    for tc in ("I", "L"):
        if array(tc).itemsize == 4:
            return tc
    raise RuntimeError("no 4-byte unsigned array typecode available")


_U32_TC = _u32_typecode()


@dataclass
class Attribute:
    name: str
    components: int
    layout: int
    stride: int   # effective byte stride between consecutive vertices
    offset: int   # byte offset from data-region start to vertex 0, component 0
    _unpack: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        self._unpack = struct.Struct("<%df" % self.components).unpack_from


@dataclass
class Primitive:
    ptype: int
    indices: object  # sequence of uint32 (memoryview, array, or list)

    def groups(self):
        """Yield the primitive as tuples of PRIM_VERTS[ptype] indices."""
        k = PRIM_VERTS[self.ptype]
        idx = self.indices
        return [tuple(idx[i:i + k]) for i in range(0, len(idx), k)]

    def indices_bytes(self) -> bytes:
        if hasattr(self.indices, "tobytes"):
            return self.indices.tobytes()
        return struct.pack("<%dI" % len(self.indices), *self.indices)


class Model:
    """A parsed VXL1 mesh: vertex attributes + indexed primitives."""

    def __init__(self, vertex_count, attributes, primitives, data_region):
        self.vertex_count = vertex_count
        self.attributes = list(attributes)
        self.primitives = list(primitives)
        self.data_region = data_region  # bytes or memoryview, zero-copy when parsed
        self._attr_by_name = {a.name: a for a in self.attributes}

    def attribute(self, name) -> Attribute:
        try:
            return self._attr_by_name[name]
        except KeyError:
            raise VXLError("no such attribute: %r" % name) from None

    def vertex(self, index, attr_name):
        """Return the attribute tuple for vertex `index`.

        The index is bounds-checked first: an out-of-range vertex is
        rejected instead of silently reading neighbouring data.
        """
        a = self.attribute(attr_name)
        if not 0 <= index < self.vertex_count:
            raise IndexOutOfBoundsError(
                "vertex index %d out of bounds (vertex_count=%d); "
                "refusing to read past the vertex table" % (index, self.vertex_count))
        return a._unpack(self.data_region, a.offset + index * a.stride)

    def iter_attr(self, attr_name):
        """Yield the attribute tuple for every vertex, in order."""
        a = self.attribute(attr_name)
        unpack = a._unpack
        region = self.data_region
        off = a.offset
        step = a.stride
        for _ in range(self.vertex_count):
            yield unpack(region, off)
            off += step

    def serialize(self) -> bytes:
        """Re-emit the canonical VXL1 byte stream.

        For a model produced by parse(), serialize() reproduces the input
        byte-for-byte (lossless round-trip).
        """
        decls = bytearray()
        for a in self.attributes:
            nb = a.name.encode("utf-8")
            decls += struct.pack("<B", len(nb)) + nb
            file_stride = a.stride if a.layout == LAYOUT_INTERLEAVED else 0
            decls += ATTR_FIXED.pack(a.components, a.layout, file_stride, a.offset)
        data_end = HEADER.size + len(decls) + len(self.data_region)
        out = bytearray()
        out += HEADER.pack(MAGIC, self.vertex_count, len(self.attributes), data_end)
        out += decls
        out += self.data_region
        out += U32.pack(len(self.primitives))
        for p in self.primitives:
            out += PRIM_HEADER.pack(p.ptype, len(p.indices))
            out += p.indices_bytes()
        return bytes(out)


def parse(data) -> Model:
    """Parse and fully validate a VXL1 byte stream."""
    buf = memoryview(data)
    size = len(buf)

    def need(off, n, what):
        if off + n > size:
            raise TruncatedDataError(
                "truncated file: need %d byte(s) for %s at file offset %d, "
                "but file is only %d bytes" % (n, what, off, size))

    need(0, HEADER.size, "header")
    magic, vcount, acount, data_end = HEADER.unpack_from(buf, 0)
    if magic != MAGIC:
        raise VXLError("bad magic %r, expected %r" % (bytes(magic), MAGIC))

    off = HEADER.size
    attrs = []
    for ai in range(acount):
        need(off, 1, "attribute #%d name length" % ai)
        nl = buf[off]
        off += 1
        need(off, nl, "attribute #%d name" % ai)
        name = bytes(buf[off:off + nl]).decode("utf-8")
        off += nl
        need(off, ATTR_FIXED.size, "attribute #%d (%r) declaration" % (ai, name))
        comps, layout, stride, offset = ATTR_FIXED.unpack_from(buf, off)
        off += ATTR_FIXED.size

        if not 1 <= comps <= 4:
            raise LayoutMismatchError(
                "attribute %r: component count %d out of range (1..4)" % (name, comps))
        if layout not in LAYOUT_NAMES:
            raise LayoutMismatchError(
                "attribute %r: unknown layout id %d" % (name, layout))
        if offset % FLOAT_SIZE:
            raise LayoutMismatchError(
                "attribute %r: offset %d is not 4-byte aligned" % (name, offset))
        if layout == LAYOUT_INTERLEAVED:
            if stride % FLOAT_SIZE:
                raise LayoutMismatchError(
                    "attribute %r: stride %d is not 4-byte aligned" % (name, stride))
            if stride < comps * FLOAT_SIZE:
                raise LayoutMismatchError(
                    "attribute %r: stride %d too small for %d components "
                    "(%d bytes); vertices would overlap" % (name, stride, comps, comps * FLOAT_SIZE))
            eff_stride = stride
        else:
            if stride != 0:
                raise LayoutMismatchError(
                    "attribute %r: planar layout must declare stride 0, got %d"
                    % (name, stride))
            eff_stride = comps * FLOAT_SIZE
        attrs.append(Attribute(name, comps, layout, eff_stride, offset))

    data_start = off
    if not data_start <= data_end <= size:
        raise VXLError(
            "data_end=%d out of range (data region starts at %d, file is %d bytes)"
            % (data_end, data_start, size))
    data_region = buf[data_start:data_end]

    # Declared components must match the data actually present, per vertex.
    for a in attrs:
        if vcount == 0:
            continue
        last_start = a.offset + (vcount - 1) * a.stride
        last_end = last_start + a.components * FLOAT_SIZE
        if last_end > len(data_region):
            avail = len(data_region) - last_start
            raise LayoutMismatchError(
                "attribute %r: declared %d components (%d bytes/vertex) but "
                "vertex %d needs data-region bytes [%d, %d) and only %d of %d "
                "bytes are available (file offset %d, data region ends at "
                "file offset %d)" % (
                    a.name, a.components, a.components * FLOAT_SIZE, vcount - 1,
                    last_start, last_end, max(avail, 0), a.components * FLOAT_SIZE,
                    data_start + last_start, data_end))

    # Primitive section.
    off = data_end
    need(off, U32.size, "primitive count")
    (pcount,) = U32.unpack_from(buf, off)
    off += U32.size
    prims = []
    for p in range(pcount):
        need(off, PRIM_HEADER.size, "primitive #%d header" % p)
        ptype, icount = PRIM_HEADER.unpack_from(buf, off)
        off += PRIM_HEADER.size
        if ptype not in PRIM_NAMES:
            raise VXLError("primitive #%d: unknown primitive type %d" % (p, ptype))
        k = PRIM_VERTS[ptype]
        if icount % k:
            raise VXLError(
                "primitive #%d (%s): index count %d is not a multiple of %d"
                % (p, PRIM_NAMES[ptype], icount, k))
        need(off, icount * 4, "primitive #%d indices" % p)
        raw = buf[off:off + icount * 4]
        off += icount * 4
        if sys.byteorder == "little":
            indices = raw.cast(_U32_TC)  # zero-copy view over the file bytes
        else:
            indices = array(_U32_TC)
            indices.frombytes(bytes(raw))
            indices.byteswap()
        # Validate every index up front: never dereference past the table.
        for j, v in enumerate(indices):
            if v >= vcount:
                raise IndexOutOfBoundsError(
                    "primitive #%d (%s) index[%d] = %d: no such vertex "
                    "(vertex_count=%d); rejected before any vertex data was read"
                    % (p, PRIM_NAMES[ptype], j, v, vcount))
        prims.append(Primitive(ptype, indices))

    if off != size:
        raise VXLError("%d trailing byte(s) after primitive section" % (size - off))
    return Model(vcount, attrs, prims, data_region)

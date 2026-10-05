"""Density-adaptive bitmap index (Roaring-style), Python 3 standard library only.

Design
------
The bit space is split into containers of 2**16 bits, keyed by the high
16 bits of a position. Each container is stored in one of two encodings:

  ARRAY  (codec 0): sorted list of 16-bit offsets, 2 bytes per set bit.
  BITMAP (codec 1): fixed 65536-bit (8192-byte) bitmask.

Selection rule (per container, see ``select_codec``):
  cardinality <= ARRAY_THRESHOLD (4096)  -> ARRAY   (2 * card bytes <= 8192)
  cardinality >  ARRAY_THRESHOLD         -> BITMAP  (8192 bytes fixed)

At the threshold both encodings cost exactly 8192 bytes; below it ARRAY
wins, above it BITMAP wins, so the rule is purely density-driven.

All set operations (AND / OR / ANDNOT) accept any mix of the two
encodings and re-normalize each result container with the same rule.
"""

from __future__ import annotations

import struct
from itertools import groupby

CHUNK_BITS = 1 << 16
CHUNK_MASK = CHUNK_BITS - 1
BITMAP_BYTES = CHUNK_BITS // 8  # 8192
ARRAY_THRESHOLD = 4096          # 2 * 4096 == 8192 == BITMAP_BYTES

CODEC_ARRAY = 0
CODEC_BITMAP = 1

_MAGIC = b"BIDX"
_VERSION = 1
_HEADER = struct.Struct("<4sBI")          # magic, version, container count
_CH_HEADER = struct.Struct("<HBI")        # high16, codec, cardinality


def select_codec(cardinality: int) -> int:
    """Density-driven encoding choice for one 65536-bit container."""
    if cardinality < 0 or cardinality > CHUNK_BITS:
        raise ValueError("cardinality out of range")
    return CODEC_ARRAY if cardinality <= ARRAY_THRESHOLD else CODEC_BITMAP


def codec_name(codec: int) -> str:
    return "ARRAY" if codec == CODEC_ARRAY else "BITMAP"


def encoded_size(cardinality: int, codec: int | None = None) -> int:
    """Payload bytes for a container with the given cardinality."""
    if codec is None:
        codec = select_codec(cardinality)
    return 2 * cardinality if codec == CODEC_ARRAY else BITMAP_BYTES


# ---------------------------------------------------------------- containers

class ArrayContainer:
    __slots__ = ("values",)

    def __init__(self, values):
        self.values = values  # sorted list of ints in [0, 65536)

    @property
    def cardinality(self):
        return len(self.values)

    def to_bitmap(self) -> "BitmapContainer":
        buf = bytearray(BITMAP_BYTES)
        for v in self.values:
            buf[v >> 3] |= 1 << (v & 7)
        return BitmapContainer(int.from_bytes(buf, "little"), len(self.values))

    def __eq__(self, other):
        return isinstance(other, ArrayContainer) and self.values == other.values


class BitmapContainer:
    __slots__ = ("bits", "cardinality")

    def __init__(self, bits: int, cardinality: int):
        self.bits = bits              # 65536-bit integer mask
        self.cardinality = cardinality

    @classmethod
    def from_values(cls, values) -> "BitmapContainer":
        buf = bytearray(BITMAP_BYTES)
        for v in values:
            buf[v >> 3] |= 1 << (v & 7)
        return cls(int.from_bytes(buf, "little"), len(values))

    def to_array(self) -> ArrayContainer:
        return ArrayContainer(_bits_to_values(self.bits))

    def to_bytearray(self) -> bytearray:
        return bytearray(self.bits.to_bytes(BITMAP_BYTES, "little"))

    def __eq__(self, other):
        return isinstance(other, BitmapContainer) and self.bits == other.bits


def _bits_to_values(bits: int) -> list[int]:
    values = []
    base = 0
    for byte in bits.to_bytes(BITMAP_BYTES, "little"):
        while byte:
            lsb = byte & -byte
            values.append(base + lsb.bit_length() - 1)
            byte ^= lsb
        base += 8
    return values


def _values_in_bytes(buf, values) -> list[int]:
    return [v for v in values if buf[v >> 3] & (1 << (v & 7))]


def _values_not_in_bytes(buf, values) -> list[int]:
    return [v for v in values if not buf[v >> 3] & (1 << (v & 7))]


def _or_values_into(bits: int, values) -> int:
    buf = bytearray(bits.to_bytes(BITMAP_BYTES, "little"))
    for v in values:
        buf[v >> 3] |= 1 << (v & 7)
    return int.from_bytes(buf, "little")


def _normalize_array(values: list[int]):
    if len(values) > ARRAY_THRESHOLD:
        return BitmapContainer.from_values(values)
    return ArrayContainer(values)


def _normalize_bitmap(bits: int, card: int):
    if card <= ARRAY_THRESHOLD:
        return ArrayContainer(_bits_to_values(bits))
    return BitmapContainer(bits, card)


# ------------------------------------------------------- container algebra

def _intersect(xs, ys):
    out = []
    i = j = 0
    nx, ny = len(xs), len(ys)
    while i < nx and j < ny:
        if xs[i] == ys[j]:
            out.append(xs[i])
            i += 1
            j += 1
        elif xs[i] < ys[j]:
            i += 1
        else:
            j += 1
    return out


def _union(xs, ys):
    out = []
    i = j = 0
    nx, ny = len(xs), len(ys)
    while i < nx and j < ny:
        if xs[i] == ys[j]:
            out.append(xs[i])
            i += 1
            j += 1
        elif xs[i] < ys[j]:
            out.append(xs[i])
            i += 1
        else:
            out.append(ys[j])
            j += 1
    out.extend(xs[i:])
    out.extend(ys[j:])
    return out


def _difference(xs, ys):
    out = []
    i = j = 0
    nx, ny = len(xs), len(ys)
    while i < nx and j < ny:
        if xs[i] == ys[j]:
            i += 1
            j += 1
        elif xs[i] < ys[j]:
            out.append(xs[i])
            i += 1
        else:
            j += 1
    out.extend(xs[i:])
    return out


def container_and(a, b):
    a_arr = isinstance(a, ArrayContainer)
    b_arr = isinstance(b, ArrayContainer)
    if a_arr and b_arr:
        return _normalize_array(_intersect(a.values, b.values))
    if not a_arr and not b_arr:
        bits = a.bits & b.bits
        return _normalize_bitmap(bits, bits.bit_count())
    arr, bmp = (a, b) if a_arr else (b, a)
    return ArrayContainer(_values_in_bytes(bmp.to_bytearray(), arr.values))


def container_or(a, b):
    a_arr = isinstance(a, ArrayContainer)
    b_arr = isinstance(b, ArrayContainer)
    if a_arr and b_arr:
        return _normalize_array(_union(a.values, b.values))
    if not a_arr and not b_arr:
        bits = a.bits | b.bits
        return _normalize_bitmap(bits, bits.bit_count())
    arr, bmp = (a, b) if a_arr else (b, a)
    bits = _or_values_into(bmp.bits, arr.values)
    return _normalize_bitmap(bits, bits.bit_count())


def container_andnot(a, b):
    a_arr = isinstance(a, ArrayContainer)
    b_arr = isinstance(b, ArrayContainer)
    if a_arr and b_arr:
        return _normalize_array(_difference(a.values, b.values))
    if not a_arr and not b_arr:
        bits = a.bits & ~b.bits
        return _normalize_bitmap(bits, bits.bit_count())
    if a_arr:  # array minus bitmap
        return ArrayContainer(_values_not_in_bytes(b.to_bytearray(), a.values))
    # bitmap minus array
    bits = a.bits
    for v in b.values:
        bits &= ~(1 << v)
    return _normalize_bitmap(bits, bits.bit_count())


# ------------------------------------------------------------------- index

class BitmapIndex:
    """A sparse bitmap index: dict of high16 -> container."""

    __slots__ = ("containers",)

    def __init__(self, containers=None):
        self.containers = containers if containers is not None else {}

    # ---- construction -------------------------------------------------

    @classmethod
    def from_positions(cls, positions) -> "BitmapIndex":
        """Build from an arbitrary iterable of bit positions (unsorted ok)."""
        return cls.from_sorted_positions(sorted(set(positions)))

    @classmethod
    def from_sorted_positions(cls, positions) -> "BitmapIndex":
        containers = {}
        for high, group in groupby(positions, key=lambda p: p >> 16):
            values = [p & CHUNK_MASK for p in group]
            containers[high] = _normalize_array(values)
        return cls(containers)

    def forced(self, codec: int) -> "BitmapIndex":
        """Return a copy with every container re-encoded as ``codec``."""
        out = {}
        for high, c in self.containers.items():
            if codec == CODEC_ARRAY:
                out[high] = c if isinstance(c, ArrayContainer) else c.to_array()
            else:
                out[high] = c if isinstance(c, BitmapContainer) else c.to_bitmap()
        return BitmapIndex(out)

    # ---- inspection ---------------------------------------------------

    def cardinality(self) -> int:
        return sum(c.cardinality for c in self.containers.values())

    def to_positions(self) -> list[int]:
        out = []
        for high in sorted(self.containers):
            c = self.containers[high]
            values = c.values if isinstance(c, ArrayContainer) else _bits_to_values(c.bits)
            base = high << 16
            out.extend(base + v for v in values)
        return out

    def codec_map(self) -> dict[int, int]:
        return {
            high: CODEC_ARRAY if isinstance(c, ArrayContainer) else CODEC_BITMAP
            for high, c in self.containers.items()
        }

    # ---- set algebra --------------------------------------------------

    def __and__(self, other: "BitmapIndex") -> "BitmapIndex":
        out = {}
        for high in self.containers.keys() & other.containers.keys():
            c = container_and(self.containers[high], other.containers[high])
            if c.cardinality:
                out[high] = c
        return BitmapIndex(out)

    def __or__(self, other: "BitmapIndex") -> "BitmapIndex":
        out = {}
        for high in self.containers.keys() | other.containers.keys():
            a = self.containers.get(high)
            b = other.containers.get(high)
            if a is None:
                out[high] = b
            elif b is None:
                out[high] = a
            else:
                out[high] = container_or(a, b)
        return BitmapIndex(out)

    def andnot(self, other: "BitmapIndex") -> "BitmapIndex":
        out = {}
        for high, a in self.containers.items():
            b = other.containers.get(high)
            c = a if b is None else container_andnot(a, b)
            if c.cardinality:
                out[high] = c
        return BitmapIndex(out)

    # ---- serialization ------------------------------------------------

    def encode(self) -> bytes:
        parts = [_HEADER.pack(_MAGIC, _VERSION, len(self.containers))]
        for high in sorted(self.containers):
            c = self.containers[high]
            if isinstance(c, ArrayContainer):
                codec = CODEC_ARRAY
                payload = struct.pack("<%dH" % len(c.values), *c.values)
            else:
                codec = CODEC_BITMAP
                payload = c.bits.to_bytes(BITMAP_BYTES, "little")
            parts.append(_CH_HEADER.pack(high, codec, c.cardinality))
            parts.append(payload)
        return b"".join(parts)

    @classmethod
    def decode(cls, data: bytes) -> "BitmapIndex":
        if len(data) < _HEADER.size:
            raise ValueError("truncated header")
        magic, version, count = _HEADER.unpack_from(data, 0)
        if magic != _MAGIC:
            raise ValueError("bad magic")
        if version != _VERSION:
            raise ValueError("unsupported version %d" % version)
        off = _HEADER.size
        containers = {}
        for _ in range(count):
            if off + _CH_HEADER.size > len(data):
                raise ValueError("truncated container header")
            high, codec, card = _CH_HEADER.unpack_from(data, off)
            off += _CH_HEADER.size
            if codec == CODEC_ARRAY:
                need = 2 * card
                if off + need > len(data):
                    raise ValueError("truncated array payload")
                values = list(struct.unpack_from("<%dH" % card, data, off))
                off += need
                if values != sorted(values) or len(set(values)) != len(values):
                    raise ValueError("array payload not sorted/unique")
                containers[high] = ArrayContainer(values)
            elif codec == CODEC_BITMAP:
                if off + BITMAP_BYTES > len(data):
                    raise ValueError("truncated bitmap payload")
                bits = int.from_bytes(data[off:off + BITMAP_BYTES], "little")
                off += BITMAP_BYTES
                if bits.bit_count() != card:
                    raise ValueError("bitmap cardinality mismatch")
                containers[high] = BitmapContainer(bits, card)
            else:
                raise ValueError("unknown codec %d" % codec)
        if off != len(data):
            raise ValueError("trailing bytes")
        return cls(containers)

    def __eq__(self, other):
        return isinstance(other, BitmapIndex) and self.to_positions() == other.to_positions()

"""VXB1 读取器：按属性布局（步长/偏移）解析，零拷贝访问。"""

import struct

from .format import (
    ATTR_FMT,
    ATTR_SIZE,
    DTYPES,
    HEADER_FMT,
    HEADER_SIZE,
    INDEX_FMT,
    INDEX_SIZE,
    LAYOUT_INTERLEAVED,
    LAYOUT_NAMES,
    LAYOUT_PLANAR,
    MAGIC,
    PRIMITIVES,
    AttributeDataError,
    FormatError,
    IndexBoundsError,
)


class AttributeInfo:
    __slots__ = ("name", "layout", "components", "dtype", "stride", "offset")

    def __init__(self, name, layout, components, dtype, stride, offset):
        self.name = name
        self.layout = layout
        self.components = components
        self.dtype = dtype
        self.stride = stride
        self.offset = offset

    @property
    def layout_name(self):
        return LAYOUT_NAMES[self.layout]

    @property
    def dtype_name(self):
        return DTYPES[self.dtype][1]

    def __repr__(self):
        return (
            "AttributeInfo(name=%r, layout=%s, components=%d, dtype=%s, "
            "stride=%d, offset=%d)"
            % (
                self.name,
                self.layout_name,
                self.components,
                self.dtype_name,
                self.stride,
                self.offset,
            )
        )


class Accessor:
    """按步长/偏移从数据区取某一个属性的逐顶点分量，不复制底层缓冲。"""

    __slots__ = ("_buf", "_offset", "_stride", "_count", "components", "_fmt")

    def __init__(self, buf, offset, stride, count, components, dtype):
        fmt_char = DTYPES[dtype][0]
        self._buf = buf
        self._offset = offset
        self._stride = stride
        self._count = count
        self.components = components
        self._fmt = "<" + fmt_char * components

    def __len__(self):
        return self._count

    def __getitem__(self, i):
        if not 0 <= i < self._count:
            raise IndexError(
                "vertex index %d out of range [0, %d)" % (i, self._count)
            )
        return struct.unpack_from(
            self._fmt, self._buf, self._offset + i * self._stride
        )

    def __iter__(self):
        for i in range(self._count):
            yield self[i]


class Mesh:
    """解析后的网格。构造时做结构校验；索引越界默认也会拒绝。"""

    def __init__(self, raw, *, strict_indices=True):
        if isinstance(raw, (bytearray, memoryview)):
            raw = bytes(raw)
        self._raw = raw
        self._parse_header()
        self._parse_attributes()
        self._validate_attributes()
        if strict_indices:
            errors = self.find_index_errors(limit=1)
            if errors:
                raise errors[0]

    # ---- 解析 ----

    def _parse_header(self):
        data = self._raw
        if len(data) < HEADER_SIZE:
            raise FormatError(
                "truncated header: need %d bytes, got %d at byte 0"
                % (HEADER_SIZE, len(data))
            )
        magic, vc, ac, ic, ptype, _ = struct.unpack_from(HEADER_FMT, data, 0)
        if magic != MAGIC:
            raise FormatError("bad magic %r at byte 0, expected %r" % (magic, MAGIC))
        if ptype not in PRIMITIVES:
            raise FormatError("unknown primitive type %d at byte 16" % ptype)
        self.vertex_count = vc
        self.index_count = ic
        self.primitive_type = ptype
        self.primitive_name, self.indices_per_primitive = PRIMITIVES[ptype]
        self._attr_count = ac

        records_end = HEADER_SIZE + ac * ATTR_SIZE
        if len(data) < records_end:
            raise FormatError(
                "truncated attribute records: need %d bytes, got %d at byte %d"
                % (records_end, len(data), HEADER_SIZE)
            )
        index_bytes = ic * INDEX_SIZE
        if len(data) < records_end + index_bytes:
            raise FormatError(
                "truncated index section: need %d index bytes at byte %d, "
                "file has %d bytes total"
                % (index_bytes, len(data) - index_bytes, len(data))
            )
        self._records_end = records_end
        # 数据区与索引区均用 memoryview 切片，零拷贝。
        view = memoryview(data)
        self._data = view[records_end : len(data) - index_bytes]
        self._index_bytes = view[len(data) - index_bytes :]
        if ic % self.indices_per_primitive:
            raise FormatError(
                "%d indices not divisible by %d per %s primitive: %d dangling"
                % (ic, self.indices_per_primitive, self.primitive_name,
                   ic % self.indices_per_primitive)
            )

    def _parse_attributes(self):
        self._attrs = []
        self._attrs_by_name = {}
        for k in range(self._attr_count):
            base = HEADER_SIZE + k * ATTR_SIZE
            name_raw, layout, comps, dtype, _flags, stride, offset, _res = (
                struct.unpack_from(ATTR_FMT, self._raw, base)
            )
            name = name_raw.split(b"\0", 1)[0].decode("ascii", "replace")
            if layout not in LAYOUT_NAMES:
                raise FormatError(
                    "attribute %r: unknown layout %d at byte %d"
                    % (name, layout, base + 16)
                )
            if not 1 <= comps <= 4:
                raise FormatError(
                    "attribute %r: invalid component count %d at byte %d"
                    % (name, comps, base + 17)
                )
            if dtype not in DTYPES:
                raise FormatError(
                    "attribute %r: unknown dtype %d at byte %d"
                    % (name, dtype, base + 18)
                )
            info = AttributeInfo(name, layout, comps, dtype, stride, offset)
            self._attrs.append(info)
            self._attrs_by_name[name] = info

    def _validate_attributes(self):
        data_len = len(self._data)
        for info in self._attrs:
            esize = DTYPES[info.dtype][2]
            need = info.components * esize
            if info.layout == LAYOUT_INTERLEAVED:
                if info.stride < need:
                    raise FormatError(
                        "attribute %r: stride %d smaller than vertex data "
                        "size %d (%d x %s)"
                        % (info.name, info.stride, need, info.components,
                           info.dtype_name)
                    )
                row = info.stride
            else:
                row = need
            if self.vertex_count == 0:
                continue
            if info.offset > data_len:
                first_bad = 0
            else:
                # 满足 offset + i*row + need <= data_len 的最大 i
                fit = (data_len - info.offset - need) // row
                first_bad = fit + 1 if fit >= 0 else 0
            if first_bad < self.vertex_count:
                byte_offset = info.offset + first_bad * row
                raise AttributeDataError(
                    info.name, first_bad, byte_offset,
                    byte_offset + need, data_len,
                )

    # ---- 访问 ----

    @property
    def attributes(self):
        return list(self._attrs)

    def attribute(self, name):
        info = self._attrs_by_name.get(name)
        if info is None:
            raise KeyError("no attribute named %r" % name)
        if info.layout == LAYOUT_PLANAR:
            stride = info.components * DTYPES[info.dtype][2]
        else:
            stride = info.stride
        return Accessor(
            self._data, info.offset, stride,
            self.vertex_count, info.components, info.dtype,
        )

    def index(self, i):
        if not 0 <= i < self.index_count:
            raise IndexError(
                "index %d out of range [0, %d)" % (i, self.index_count)
            )
        return struct.unpack_from(INDEX_FMT, self._index_bytes, i * INDEX_SIZE)[0]

    def iter_indices(self):
        for (v,) in struct.iter_unpack(INDEX_FMT, self._index_bytes):
            yield v

    def primitives(self):
        per = self.indices_per_primitive
        it = self.iter_indices()
        while True:
            prim = []
            for _ in range(per):
                try:
                    prim.append(next(it))
                except StopIteration:
                    return
            yield tuple(prim)

    # ---- 校验 ----

    def find_index_errors(self, limit=None):
        """返回所有越界索引错误（含图元号与索引值）。"""
        errors = []
        per = self.indices_per_primitive
        for i, v in enumerate(self.iter_indices()):
            if v >= self.vertex_count:
                errors.append(
                    IndexBoundsError(
                        i // per, self.primitive_name, i % per,
                        v, self.vertex_count,
                    )
                )
                if limit is not None and len(errors) >= limit:
                    break
        return errors

    # ---- 写出 ----

    def to_bytes(self):
        """从解析结果重建文件字节；未修改时与原始输入逐字节一致。"""
        parts = [
            struct.pack(
                HEADER_FMT, MAGIC, self.vertex_count, len(self._attrs),
                self.index_count, self.primitive_type, b"\0" * 3,
            )
        ]
        for info in self._attrs:
            encoded = info.name.encode("ascii")
            parts.append(
                struct.pack(
                    ATTR_FMT,
                    encoded + b"\0" * (16 - len(encoded)),
                    info.layout, info.components, info.dtype, 0,
                    info.stride, info.offset, 0,
                )
            )
        parts.append(self._data)
        parts.append(self._index_bytes)
        return b"".join(parts)

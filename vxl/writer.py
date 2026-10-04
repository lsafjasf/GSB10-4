"""VXB1 文件装配器与两种布局的数据打包辅助函数。"""

import struct

from .format import (
    ATTR_FMT,
    DTYPES,
    HEADER_FMT,
    LAYOUT_INTERLEAVED,
    LAYOUT_PLANAR,
    MAGIC,
    DTYPE_BY_NAME,
    PRIMITIVE_BY_NAME,
)


def _dtype_code(dtype):
    return DTYPE_BY_NAME[dtype] if isinstance(dtype, str) else dtype


def assemble_file(*, vertex_count, attributes, primitive, data_section, indices):
    """按声明与数据区装配一个 VXB1 文件。

    attributes: [(name, layout, components, dtype, stride, offset), ...]
      layout/dtype/primitive 可传字符串或数字常量。
    data_section: bytes，所有属性块共用的数据区。
    indices: 可迭代的 int。
    """
    records = []
    for name, layout, components, dtype, stride, offset in attributes:
        if isinstance(layout, str):
            layout = {"interleaved": LAYOUT_INTERLEAVED,
                      "planar": LAYOUT_PLANAR}[layout]
        dtype = _dtype_code(dtype)
        encoded = name.encode("ascii")
        if len(encoded) > 16:
            raise ValueError("attribute name too long: %r" % name)
        records.append(
            struct.pack(
                ATTR_FMT,
                encoded + b"\0" * (16 - len(encoded)),
                layout, components, dtype, 0, stride, offset, 0,
            )
        )
    if isinstance(primitive, str):
        ptype = PRIMITIVE_BY_NAME[primitive]
    else:
        ptype = primitive
    indices = list(indices)
    header = struct.pack(
        HEADER_FMT, MAGIC, vertex_count, len(records),
        len(indices), ptype, b"\0" * 3,
    )
    index_bytes = struct.pack("<%dI" % len(indices), *indices) if indices else b""
    return header + b"".join(records) + bytes(data_section) + index_bytes


def planar_layout(vertices, attr_specs):
    """分行布局: 每个属性一块, 顶点分量紧密排列。

    vertices: list[dict[name, tuple]]，attr_specs: [(name, components, dtype)]
    返回 (data_section, {name: (layout, stride, offset)})。
    """
    blocks = []
    offsets = {}
    packed = {}
    for name, components, dtype in attr_specs:
        fmt_char = DTYPES[_dtype_code(dtype)][0]
        offsets[name] = sum(len(b) for b in blocks)
        out = bytearray()
        fmt = "<" + fmt_char * components
        for v in vertices:
            values = v[name]
            if len(values) != components:
                raise ValueError(
                    "attribute %r expects %d components, got %d"
                    % (name, components, len(values))
                )
            out += struct.pack(fmt, *values)
        packed[name] = bytes(out)
        blocks.append(packed[name])
    result = {}
    for name, components, dtype in attr_specs:
        stride = components * DTYPES[_dtype_code(dtype)][2]
        result[name] = (LAYOUT_PLANAR, stride, offsets[name])
    return b"".join(blocks), result


def interleaved_layout(vertices, attr_specs, *, align=4):
    """交错布局: 属性按声明顺序放在同一顶点步长内，可尾部填充对齐。

    返回 (data_section, {name: (layout, stride, offset)})。
    """
    offsets = {}
    cursor = 0
    for name, components, dtype in attr_specs:
        offsets[name] = cursor
        cursor += components * DTYPES[_dtype_code(dtype)][2]
    stride = cursor
    if align and stride % align:
        stride += align - stride % align
    out = bytearray(len(vertices) * stride)
    for i, v in enumerate(vertices):
        base = i * stride
        for name, components, dtype in attr_specs:
            fmt_char = DTYPES[_dtype_code(dtype)][0]
            struct.pack_into(
                "<" + fmt_char * components, out, base + offsets[name], *v[name]
            )
    result = {
        name: (LAYOUT_INTERLEAVED, stride, offsets[name])
        for name, _components, _dtype in attr_specs
    }
    return bytes(out), result


def mixed_layout(vertices, groups):
    """混用布局: groups 为多组属性, 每组内部按选定布局打包, 各组块前后拼接。

    groups: [("interleaved", specs) | ("planar", specs), ...]
    返回 (data_section, {name: (layout, stride, offset)})。
    """
    blocks = []
    layout_info = {}
    for kind, specs in groups:
        if kind == "interleaved":
            block, info = interleaved_layout(vertices, specs)
        elif kind == "planar":
            block, info = planar_layout(vertices, specs)
        else:
            raise ValueError("unknown group kind %r" % kind)
        shift = sum(len(b) for b in blocks)
        for name, (layout, stride, offset) in info.items():
            layout_info[name] = (layout, stride, offset + shift)
        blocks.append(block)
    return b"".join(blocks), layout_info


def build(vertices, attr_specs, *, layout, groups=None, primitive="triangles",
          indices=None):
    """高层构建: 生成一个完整 VXB1 文件字节串。

    layout: "interleaved" | "planar" | "mixed"
    mixed 时通过 groups 传入分组（见 mixed_layout）。
    """
    if layout == "interleaved":
        data, info = interleaved_layout(vertices, attr_specs)
    elif layout == "planar":
        data, info = planar_layout(vertices, attr_specs)
    elif layout == "mixed":
        if groups is None:
            raise ValueError("mixed layout requires groups")
        data, info = mixed_layout(vertices, groups)
    else:
        raise ValueError("unknown layout %r" % layout)
    if indices is None:
        primitive_code = PRIMITIVE_BY_NAME[primitive]
        per = {1: 1, 2: 2, 3: 3, 4: 4}[primitive_code]
        if len(vertices) % per:
            raise ValueError("vertex count not divisible by primitive size")
        indices = list(range(len(vertices)))
    attrs = [
        (name, layout_, components, dtype, stride, offset)
        for name, components, dtype in attr_specs
        for (layout_, stride, offset) in [info[name]]
    ]
    return assemble_file(
        vertex_count=len(vertices), attributes=attrs, primitive=primitive,
        data_section=data, indices=indices,
    )

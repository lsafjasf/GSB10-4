"""VXB1 二进制网格格式常量与错误类型。

文件布局（小端）::

    header          20 字节: magic "VXB1" | u32 vertex_count | u32 attr_count
                             | u32 index_count | u8 primitive_type | 3 保留
    attr records    每个 32 字节: 16 字节 name(NUL 填充) | u8 layout
                             | u8 components | u8 dtype | u8 flags
                             | u32 stride | u32 offset | u32 reserved
    data section    属性数据区（交错块或分行块，按 offset 引用）
    index section   index_count 个 u32

交错(interleaved)属性: offset 为块内首顶点偏移, stride 为顶点步长。
分行(planar)属性:     offset 为块起始, 每顶点 components*elem_size 字节紧密排列。
"""

import struct

MAGIC = b"VXB1"

LAYOUT_INTERLEAVED = 0
LAYOUT_PLANAR = 1
LAYOUT_NAMES = {LAYOUT_INTERLEAVED: "interleaved", LAYOUT_PLANAR: "planar"}

# dtype -> (struct 字符, 名称, 字节数)
DTYPES = {
    0: ("f", "float32", 4),
    1: ("H", "uint16", 2),
    2: ("I", "uint32", 4),
    3: ("h", "int16", 2),
}
DTYPE_BY_NAME = {name: code for code, (_, name, _) in DTYPES.items()}

# primitive_type -> (名称, 每图元顶点数)
PRIMITIVES = {
    1: ("points", 1),
    2: ("lines", 2),
    3: ("triangles", 3),
    4: ("quads", 4),
}
PRIMITIVE_BY_NAME = {name: code for code, (name, _) in PRIMITIVES.items()}

HEADER_FMT = "<4sIIIB3s"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 20

ATTR_FMT = "<16sBBBBIII"
ATTR_SIZE = struct.calcsize(ATTR_FMT)  # 32

INDEX_FMT = "<I"
INDEX_SIZE = 4


class VXLError(Exception):
    """所有 vxl 错误的基类。"""


class FormatError(VXLError):
    """文件结构层面的错误（魔数、截断、非法声明等）。"""


class AttributeDataError(VXLError):
    """属性声明与数据长度不匹配。"""

    def __init__(self, name, vertex, byte_offset, needed_end, available):
        self.name = name
        self.vertex = vertex
        self.byte_offset = byte_offset
        super().__init__(
            "attribute %r: declared data overruns data section at vertex %d "
            "(byte offset %d, need data through byte %d, data section has %d bytes)"
            % (name, vertex, byte_offset, needed_end, available)
        )


class IndexBoundsError(VXLError):
    """索引引用了不存在的顶点。"""

    def __init__(self, prim_index, prim_name, corner, value, vertex_count):
        self.prim_index = prim_index
        self.corner = corner
        self.value = value
        super().__init__(
            "index out of range: primitive #%d (%s), corner %d: "
            "index %d >= vertex_count %d"
            % (prim_index, prim_name, corner, value, vertex_count)
        )

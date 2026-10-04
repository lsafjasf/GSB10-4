"""vxl: 顶点属性布局解析库（交错 / 分行布局，步长与偏移取数）。"""

from .format import (
    LAYOUT_INTERLEAVED,
    LAYOUT_PLANAR,
    AttributeDataError,
    FormatError,
    IndexBoundsError,
    VXLError,
)
from .reader import Accessor, AttributeInfo, Mesh
from .writer import assemble_file, build

__all__ = [
    "Accessor",
    "AttributeInfo",
    "Mesh",
    "VXLError",
    "FormatError",
    "AttributeDataError",
    "IndexBoundsError",
    "LAYOUT_INTERLEAVED",
    "LAYOUT_PLANAR",
    "assemble_file",
    "build",
]

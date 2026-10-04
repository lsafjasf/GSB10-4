"""书签解析与还原库（仅标准库）。"""

from .iterjson import JSONParseError, dumps, loads
from .model import (
    BookmarkDocument,
    BookmarkFormatError,
    Issue,
    Report,
    FORMAT_ID,
    FORMAT_VERSION,
    ROOT_PARENT_ID,
)

__all__ = [
    "JSONParseError",
    "dumps",
    "loads",
    "BookmarkDocument",
    "BookmarkFormatError",
    "Issue",
    "Report",
    "FORMAT_ID",
    "FORMAT_VERSION",
    "ROOT_PARENT_ID",
]

__version__ = "1.0.0"

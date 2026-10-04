"""Icon theme directory-convention validation and fallback-chain resolution."""

from .theme import (
    ChainEntry,
    CorruptFile,
    DirCache,
    Hit,
    Query,
    Resolution,
    Resolver,
    check_icon_file,
    read_index,
)
from .validate import (
    CorruptEntry,
    Problem,
    ThemeReport,
    validate_icons,
    validate_theme,
)

__all__ = [
    "ChainEntry", "CorruptEntry", "CorruptFile", "DirCache", "Hit",
    "Problem", "Query", "Resolution", "Resolver", "ThemeReport",
    "check_icon_file", "read_index", "validate_icons", "validate_theme",
]

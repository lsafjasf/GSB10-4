"""sstlib: shared string table parsing and cell restoration for XLSX packages.

Standard library only. See README.md for usage.
"""

from .errors import SstError, MissingSharedStringsError, SharedStringIndexError
from .model import RichText, Cell, Sheet
from .parser import (
    parse_shared_strings,
    parse_worksheet,
    load_workbook,
    iter_range,
    col_to_index,
    index_to_col,
    split_coord,
)
from .builder import build_xlsx, sheet_xml, shared_strings_xml

__all__ = [
    "SstError",
    "MissingSharedStringsError",
    "SharedStringIndexError",
    "RichText",
    "Cell",
    "Sheet",
    "parse_shared_strings",
    "parse_worksheet",
    "load_workbook",
    "iter_range",
    "col_to_index",
    "index_to_col",
    "split_coord",
    "build_xlsx",
    "sheet_xml",
    "shared_strings_xml",
]

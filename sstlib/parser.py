"""Streaming XLSX shared-string / worksheet parser (standard library only).

Memory usage is linear in the number of stored entries and explicit cells:
shared strings are collected into a list, worksheet cells into a sparse dict
keyed by coordinate. No grid of (max_row * max_col) is ever allocated.
"""

import re
import xml.sax
import xml.sax.handler
import zipfile
from xml.etree import ElementTree

from .errors import (
    MissingSharedStringsError,
    SharedStringIndexError,
    SstError,
)
from .model import Cell, Sheet
from .sicollector import SiCollector, localname

_COORD_RE = re.compile(r"^([A-Za-z]+)([1-9][0-9]*)$")
_REF_RE = re.compile(r"\s+")


def col_to_index(letters):
    """``A`` -> 0, ``B`` -> 1, ``AA`` -> 26 (zero based)."""
    index = 0
    for ch in letters.upper():
        if not ("A" <= ch <= "Z"):
            raise ValueError("bad column letters: %r" % letters)
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def index_to_col(index):
    """Inverse of :func:`col_to_index`."""
    if index < 0:
        raise ValueError("column index must be >= 0")
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def split_coord(coord):
    """``"B12"`` -> ``(1, 11)`` (zero based col, zero based row)."""
    match = _COORD_RE.match(coord)
    if not match:
        raise ValueError("bad cell coordinate: %r" % coord)
    return col_to_index(match.group(1)), int(match.group(2)) - 1


def make_coord(col_index, row_index):
    return "%s%d" % (index_to_col(col_index), row_index + 1)


def iter_range(top_left, bottom_right):
    """Yield every coordinate in the explicit inclusive range, row major."""
    c0, r0 = split_coord(top_left)
    c1, r1 = split_coord(bottom_right)
    if c1 < c0 or r1 < r0:
        raise ValueError("inverted range %s:%s" % (top_left, bottom_right))
    for row in range(r0, r1 + 1):
        for col in range(c0, c1 + 1):
            yield make_coord(col, row)


def parse_ref(ref):
    """``"A1:C3"`` -> ``("A1", "C3")``; a single coord counts as 1x1."""
    parts = [p for p in _REF_RE.sub(" ", ref).split() if p]
    if not parts:
        raise ValueError("empty range reference")
    if ":" in ref:
        left, right = ref.split(":", 1)
        left, right = left.strip(), right.strip()
        split_coord(left)
        split_coord(right)
        return left, right
    coord = parts[0]
    split_coord(coord)
    return coord, coord


def _open_source(source):
    """Accept a path or a binary file object; return a binary stream."""
    if hasattr(source, "read"):
        return source
    return open(source, "rb")


def parse_shared_strings(source):
    """Parse ``sharedStrings.xml`` into a list of strings / RichText items."""
    handler = _SstHandler()
    parser = xml.sax.make_parser()
    parser.setFeature(xml.sax.handler.feature_namespaces, False)
    parser.setContentHandler(handler)
    stream = _open_source(source)
    try:
        parser.parse(stream)
    finally:
        if stream is not source:
            stream.close()
    return handler.entries


class _SstHandler(xml.sax.handler.ContentHandler):
    def __init__(self):
        super().__init__()
        self.entries = []
        self.current = None

    def startElement(self, name, attrs):
        tag = localname(name)
        if tag == "si":
            self.current = SiCollector()
        elif self.current is not None:
            self.current.start(name, attrs)

    def characters(self, content):
        if self.current is not None:
            self.current.chars(content)

    def endElement(self, name):
        if self.current is None:
            return
        self.current.end(name)
        if self.current.finished:
            self.entries.append(self.current.result())
            self.current = None


def parse_worksheet(source, name, strings=None, sheet_id=None,
                    rel_id=None, path=None):
    """Parse one ``sheetN.xml`` into a :class:`Sheet`.

    ``strings`` is the parsed shared string table (list). Cells of type
    ``s`` are restored against it; a missing table or an index outside
    ``range(len(strings))`` raises with the sheet name and coordinate.
    """
    handler = _SheetHandler(name, strings)
    parser = xml.sax.make_parser()
    parser.setFeature(xml.sax.handler.feature_namespaces, False)
    parser.setContentHandler(handler)
    stream = _open_source(source)
    try:
        parser.parse(stream)
    finally:
        if stream is not source:
            stream.close()
    return Sheet(
        name=name,
        sheet_id=sheet_id,
        rel_id=rel_id,
        cells=handler.cells,
        merges=handler.merges,
        path=path,
    )


class _SheetHandler(xml.sax.handler.ContentHandler):
    def __init__(self, sheet_name, strings):
        super().__init__()
        self.sheet_name = sheet_name
        self.strings = strings
        self.cells = {}
        self.merges = []
        self._coord = None
        self._ctype = None
        self._style = None
        self._value = None       # list while inside <v>
        self._formula = None     # list while inside <f>
        self._inline = None      # SiCollector while inside <is>

    def startElement(self, name, attrs):
        tag = localname(name)
        if tag == "c":
            self._coord = attrs.get("r")
            self._ctype = attrs.get("t")
            style = attrs.get("s")
            self._style = None if style is None else style
            self._value = None
            self._formula = None
            self._inline = None
        elif tag == "v":
            self._value = []
        elif tag == "f":
            self._formula = []
        elif tag == "is":
            self._inline = SiCollector()
        elif self._inline is not None:
            self._inline.start(name, attrs)
        elif tag == "mergeCell":
            ref = attrs.get("ref")
            if not ref:
                raise SstError(
                    "mergeCell without ref attribute",
                    sheet=self.sheet_name,
                )
            try:
                self.merges.append(parse_ref(ref))
            except ValueError as exc:
                raise SstError(
                    "bad merge reference %r: %s" % (ref, exc),
                    sheet=self.sheet_name,
                ) from exc

    def characters(self, content):
        if self._inline is not None:
            self._inline.chars(content)
        elif self._value is not None:
            self._value.append(content)
        elif self._formula is not None:
            self._formula.append(content)

    def endElement(self, name):
        tag = localname(name)
        if tag == "c":
            cell = self._finish_cell()
            if self._coord is None:
                raise SstError(
                    "cell without r (coordinate) attribute",
                    sheet=self.sheet_name,
                )
            if self._coord in self.cells:
                raise SstError(
                    "duplicate cell coordinate %s" % self._coord,
                    sheet=self.sheet_name,
                    coord=self._coord,
                )
            self.cells[self._coord] = cell
            self._coord = None
            self._ctype = None
            self._style = None
            self._value = None
            self._formula = None
            self._inline = None
        elif tag == "v":
            self._value = "".join(self._value)
        elif tag == "f":
            self._formula = "".join(self._formula)
        elif tag == "is":
            self._inline.end(name)
        elif self._inline is not None:
            self._inline.end(name)

    def _finish_cell(self):
        ctype = self._ctype
        value = self._value
        formula = self._formula
        style = self._style

        if ctype == "s":
            if self.strings is None:
                raise MissingSharedStringsError(
                    "cell references shared string table (t=\"s\") but "
                    "xl/sharedStrings.xml is absent from the package",
                    sheet=self.sheet_name,
                    coord=self._coord,
                )
            if value is None:
                raise SstError(
                    "shared-string cell without <v> index",
                    sheet=self.sheet_name,
                    coord=self._coord,
                )
            try:
                index = int("".join(value.split()))
            except ValueError:
                raise SstError(
                    "shared-string index %r is not an integer" % value,
                    sheet=self.sheet_name,
                    coord=self._coord,
                )
            if index < 0 or index >= len(self.strings):
                raise SharedStringIndexError(
                    index,
                    len(self.strings),
                    sheet=self.sheet_name,
                    coord=self._coord,
                )
            return Cell("str", self.strings[index], style=style,
                        formula=formula)

        if ctype == "inlineStr":
            value = self._inline.result() if self._inline is not None else ""
            return Cell("str", value, style=style, formula=formula)

        if ctype == "b":
            return Cell("bool", value, style=style, formula=formula)

        if ctype == "str":
            return Cell("str", value, style=style, formula=formula)

        if value is None and formula is None:
            return Cell("empty", None, style=style)

        return Cell("number", value, style=style, formula=formula)


_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _norm_target(target):
    target = target.replace("\\", "/")
    if target.startswith("/"):
        return target.lstrip("/")
    if not target.startswith("xl/"):
        target = "xl/" + target
    return target


def load_workbook(path):
    """Load every sheet of an xlsx package.

    Returns an ordered dict ``{sheet_name: Sheet}``. Raises the errors from
    :mod:`sstlib.errors` (all carrying sheet name + coordinate) when shared
    string resolution fails.
    """
    with zipfile.ZipFile(path) as zf:
        with zf.open("xl/workbook.xml") as fh:
            wb_root = ElementTree.fromstring(fh.read())
        rels = {}
        try:
            with zf.open("xl/_rels/workbook.xml.rels") as fh:
                rel_root = ElementTree.fromstring(fh.read())
        except KeyError:
            rel_root = None
        if rel_root is not None:
            for rel in rel_root:
                rid = rel.get("Id")
                target = rel.get("Target")
                if rid and target:
                    rels[rid] = _norm_target(target)

        strings = None
        if "xl/sharedStrings.xml" in zf.namelist():
            with zf.open("xl/sharedStrings.xml") as fh:
                strings = parse_shared_strings(fh)

        sheets = {}
        for elem in wb_root.iter():
            if localname(elem.tag) != "sheet":
                continue
            sheet_name = elem.get("name")
            sheet_id = elem.get("sheetId")
            rid = elem.get("{%s}id" % _DOC_NS)
            target = rels.get(rid if rid is not None else "")
            if target is None:
                raise SstError(
                    "worksheet relationship %r not found" % rid,
                    sheet=sheet_name,
                )
            with zf.open(target) as fh:
                sheet = parse_worksheet(
                    fh, sheet_name, strings=strings,
                    sheet_id=sheet_id, rel_id=rid, path=target,
                )
            sheets[sheet_name] = sheet
        return sheets

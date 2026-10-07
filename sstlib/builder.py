"""Build minimal but spec-shaped xlsx packages (stdlib only).

Used to produce cross-check fixtures and benchmark data. Two encodings of
the same logical sheet are supported:

* compressed  - strings deduplicated into ``xl/sharedStrings.xml``, cells
                store integer indexes (``t="s"``);
* inline      - every string cell stores its text uncompressed as
                ``<is><t>...</t></is>`` (``t="inlineStr"``).
"""

import zipfile
from xml.sax.saxutils import escape, quoteattr

from .model import RichText

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    'content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats'
    '-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application'
    '/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/sharedStrings.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
    '</Types>'
)

ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/officeDocument" Target="xl/'
    'workbook.xml"/>'
    '</Relationships>'
)

WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/'
    'main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships">'
    '<sheets><sheet name={name} sheetId="1" r:id="rId1"/></sheets>'
    '</workbook>'
)

WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/worksheet" Target="worksheets/'
    'sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/sharedStrings" Target="'
    'sharedStrings.xml"/>'
    '</Relationships>'
)

MAIN_NS = (
    "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
)


def _text_element(text):
    attrs = ""
    if text != text.strip():
        attrs = ' xml:space="preserve"'
    return "<t%s>%s</t>" % (attrs, escape(text))


def _props_xml(props):
    parts = []
    for tag, attrs in props.items():
        attr_xml = "".join(
            " %s=%s" % (key, quoteattr(str(val)))
            for key, val in attrs.items()
        )
        parts.append("<%s%s/>" % (tag, attr_xml))
    return "".join(parts)


def si_xml(value):
    """Serialize one shared string (str or RichText) as ``<si>``."""
    if isinstance(value, RichText):
        parts = ["<si>"]
        for text, props in value.runs:
            parts.append("<r>")
            if props:
                parts.append("<rPr>%s</rPr>" % _props_xml(props))
            parts.append(_text_element(text))
            parts.append("</r>")
        parts.append("</si>")
        return "".join(parts)
    return "<si>%s</si>" % _text_element(value)


def shared_strings_xml(strings):
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n',
        '<sst xmlns="%s" count="%d" uniqueCount="%d">'
        % (MAIN_NS, len(strings), len(strings)),
    ]
    parts.extend(si_xml(s) for s in strings)
    parts.append("</sst>")
    return "".join(parts)


def _cell_xml(coord, value, compressed, string_index):
    """Serialize one cell.

    ``value`` may be str / RichText (string cell), int/float (number),
    bool, or None (explicit empty cell).
    """
    if value is None:
        return "<c r=%s/>" % quoteattr(coord)
    if isinstance(value, bool):
        return '<c r=%s t="b"><v>%d</v></c>' % (
            quoteattr(coord), 1 if value else 0,
        )
    if isinstance(value, (int, float)):
        return "<c r=%s><v>%r</v></c>" % (quoteattr(coord), value)
    if compressed:
        return '<c r=%s t="s"><v>%d</v></c>' % (
            quoteattr(coord), string_index[value],
        )
    if isinstance(value, RichText):
        inner = "".join(
            "<r>%s%s</r>"
            % (
                "<rPr>%s</rPr>" % _props_xml(props) if props else "",
                _text_element(text),
            )
            for text, props in value.runs
        )
    else:
        inner = _text_element(value)
    return '<c r=%s t="inlineStr"><is>%s</is></c>' % (
        quoteattr(coord), inner,
    )


def sheet_xml(cells, compressed=True, string_index=None, merges=(),
              dimension=None):
    """Serialize a worksheet.

    ``cells`` maps coordinate -> value (see :func:`_cell_xml`). ``merges``
    is an iterable of explicit ``"A1:B2"`` range strings.
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n',
        '<worksheet xmlns="%s">' % MAIN_NS,
    ]
    if dimension:
        parts.append('<dimension ref="%s"/>' % dimension)
    parts.append("<sheetData>")
    def sort_key(coord):
        from .parser import split_coord
        col, row = split_coord(coord)
        return (row, col)

    for coord in sorted(cells, key=sort_key):
        parts.append(
            _cell_xml(coord, cells[coord], compressed, string_index)
        )
    parts.append("</sheetData>")
    if merges:
        parts.append('<mergeCells count="%d">' % len(merges))
        for ref in merges:
            parts.append('<mergeCell ref="%s"/>' % escape(ref))
        parts.append("</mergeCells>")
    parts.append("</worksheet>")
    return "".join(parts)


def build_xlsx(path, cells, compressed=True, strings=None, merges=(),
               sheet_name="Sheet1", dimension=None, include_sst=True):
    """Write a complete xlsx package.

    When ``compressed`` is true, ``strings`` (list) is deduplicated into the
    shared string table and cells reference it by index. Otherwise strings
    are written inline and no sharedStrings part is emitted unless
    ``include_sst`` is set.
    """
    string_index = None
    if compressed:
        if strings is None:
            raise ValueError("compressed build needs a string table")
        string_index = {}
        for idx, value in enumerate(strings):
            string_index.setdefault(value, idx)
    sheet = sheet_xml(
        cells, compressed=compressed, string_index=string_index,
        merges=merges, dimension=dimension,
    )
    workbook = WORKBOOK.replace("{name}", quoteattr(sheet_name))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
        if include_sst and (compressed or strings is not None):
            zf.writestr(
                "xl/sharedStrings.xml",
                shared_strings_xml(strings or []),
            )
    return path

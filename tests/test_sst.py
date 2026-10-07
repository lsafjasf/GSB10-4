"""Self tests for sstlib: streamable restoration, errors, merges, sparsity."""

import io
import unittest

from sstlib import (
    MissingSharedStringsError,
    RichText,
    SharedStringIndexError,
    SstError,
    col_to_index,
    index_to_col,
    iter_range,
    parse_shared_strings,
    parse_worksheet,
    sheet_xml,
    shared_strings_xml,
    split_coord,
)
from sstlib.builder import si_xml

NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def sst_bytes(strings):
    return shared_strings_xml(strings).encode()


def sheet_bytes(cells, strings=None, inline=False, merges=(), dimension=None):
    if inline:
        xml = sheet_xml(cells, compressed=False, merges=merges,
                        dimension=dimension)
    else:
        index = {}
        for idx, value in enumerate(strings):
            index.setdefault(value, idx)
        xml = sheet_xml(cells, compressed=True, string_index=index,
                        merges=merges, dimension=dimension)
    return xml.encode()


class CoordinateTests(unittest.TestCase):
    def test_column_roundtrip(self):
        for letters in ["A", "B", "Z", "AA", "AZ", "ZZ", "AAA", "XFD"]:
            self.assertEqual(index_to_col(col_to_index(letters)), letters)
        self.assertEqual((col_to_index("A"), 0), (0, 0))
        self.assertEqual(col_to_index("AA"), 26)
        self.assertEqual(split_coord("B12"), (1, 11))

    def test_iter_range_explicit_only(self):
        self.assertEqual(
            list(iter_range("B2", "C3")),
            ["B2", "C2", "B3", "C3"],
        )
        self.assertEqual(list(iter_range("A1", "A1")), ["A1"])
        with self.assertRaises(ValueError):
            list(iter_range("C3", "A1"))


class RichTextTests(unittest.TestCase):
    RICH = RichText([
        ("plain ", {}),
        ("bold", {"b": {}, "color": {"rgb": "FF0000FF"}}),
        (" italic", {"i": {}}),
    ])

    def test_runs_join_in_order_with_marks(self):
        parsed = parse_shared_strings(io.BytesIO(sst_bytes([self.RICH])))
        item = parsed[0]
        self.assertIsInstance(item, RichText)
        self.assertEqual(item.text, "plain bold italic")
        self.assertEqual(item.runs, self.RICH.runs)
        self.assertEqual(item.runs[1][1]["color"], {"rgb": "FF0000FF"})
        self.assertEqual(item.runs[2][1], {"i": {}})

    def test_phonetic_runs_skipped_text_kept(self):
        xml = (
            '<?xml version="1.0"?><sst %s><si>'
            '<r><rPr><b/></rPr><t>漢字</t></r>'
            '<rPh sb="0" eb="2"><t>KANJI-IGNORED</t></rPh>'
            '<phoneticPr fontId="3"/>'
            '<r><t>ok</t></r>'
            '</si></sst>' % NS
        )
        parsed = parse_shared_strings(io.BytesIO(xml.encode()))
        self.assertEqual(parsed[0].text, "漢字ok")
        self.assertEqual(parsed[0].runs[0][1], {"b": {}})

    def test_plain_string_preserved_whitespace(self):
        parsed = parse_shared_strings(io.BytesIO(sst_bytes(["  hi "])))
        self.assertEqual(parsed[0], "  hi ")

    def test_empty_si(self):
        parsed = parse_shared_strings(
            io.BytesIO(('<?xml version="1.0"?><sst %s><si/></sst>' % NS)
                       .encode())
        )
        self.assertEqual(parsed, [""])

    def test_si_xml_roundtrip_unicode(self):
        parsed = parse_shared_strings(
            io.BytesIO(si_xml("表名/坐标 🎉").encode())
        )
        self.assertEqual(parsed[0] if isinstance(parsed, list) else parsed,
                         "表名/坐标 🎉")


class CellRestorationTests(unittest.TestCase):
    STRINGS = ["a", "b", "c", "same",
               RichText([("x", {"b": {}}), ("y", {"i": {}})])]

    def _parse(self, cells, **kwargs):
        return parse_worksheet(
            io.BytesIO(sheet_bytes(cells, self.STRINGS, **kwargs)),
            "T", strings=self.STRINGS,
        )

    def test_shared_and_duplicate_index(self):
        sheet = self._parse({"A1": "a", "B1": "a", "C1": "c"})
        self.assertEqual(sheet.cells["A1"].value, "a")
        self.assertEqual(sheet.cells["B1"].value, "a")
        self.assertEqual(sheet.cells["C1"].value, "c")

    def test_rich_cell_restored_with_format(self):
        sheet = self._parse({"D9": self.STRINGS[4]})
        cell = sheet.cells["D9"]
        self.assertIsInstance(cell.value, RichText)
        self.assertEqual(cell.plain_text(), "xy")
        self.assertEqual(cell.value.runs[0][1], {"b": {}})

    def test_sparse_coordinates_not_filled(self):
        sheet = self._parse({"A1": "a", "ZZ100000": "b"}, dimension="A1:ZZ100000")
        self.assertEqual(set(sheet.cells), {"A1", "ZZ100000"})

    def test_explicit_empty_cell_stored(self):
        sheet = self._parse({"A1": None, "B1": "a"})
        self.assertEqual(sheet.cells["A1"].kind, "empty")
        self.assertEqual(sheet.cells["B1"].kind, "str")

    def test_missing_region_is_not_synthesized(self):
        sheet = self._parse({"A1": "a"}, merges=["C5:D6"])
        with self.assertRaises(KeyError):
            sheet.get("B2")
        merged = dict(sheet.merged_coords())
        self.assertEqual(
            merged["C5:D6"], ["C5", "D5", "C6", "D6"]
        )

    def test_numeric_and_bool(self):
        sheet = self._parse({"A1": 12, "A2": 3.5, "A3": True, "A4": False})
        self.assertEqual(sheet.cells["A1"].kind, "number")
        self.assertEqual(sheet.cells["A2"].value, "3.5")
        self.assertEqual(sheet.cells["A3"].kind, "bool")
        self.assertEqual(sheet.cells["A3"].value, "1")

    def test_whole_column_same_text(self):
        cells = {"A%d" % row: "same" for row in range(1, 1001)}
        sheet = self._parse(cells)
        self.assertEqual(len(sheet.cells), 1000)
        self.assertTrue(
            all(c.value == "same" for c in sheet.cells.values())
        )

    def test_single_cell_sheet(self):
        sheet = self._parse({"E5": "a"})
        self.assertEqual(list(sheet.cells), ["E5"])


class CompressedVsInlineTests(unittest.TestCase):
    """Parse results must match cell by cell between the two encodings."""

    def _logical_cells(self):
        return {
            "A1": "alpha", "B1": "alpha", "C1": "beta",
            "A2": None, "B2": 7, "C2": 0.25,
            "A3": True,
            "A5": "  spaced  ",
            "D8": RichText([("bo", {"b": {}}), ("ld", {})]),
            "Z99": "unicode 表格 中文",
        }

    def _canonical(self, sheet):
        out = {}
        for coord, cell in sheet.cells.items():
            value = cell.value
            if isinstance(value, RichText):
                value = {"runs": [[t, p] for t, p in value.runs]}
            out[coord] = {"kind": cell.kind, "value": value}
        return out

    def test_cell_by_cell_equal(self):
        strings = ["alpha", "beta", "  spaced  ",
                   RichText([("bo", {"b": {}}), ("ld", {})]),
                   "unicode 表格 中文"]
        cells = self._logical_cells()
        comp = parse_worksheet(
            io.BytesIO(sheet_bytes(cells, strings, merges=["A1:B1"])),
            "Compressed", strings=strings,
        )
        inline = parse_worksheet(
            io.BytesIO(sheet_bytes(cells, inline=True, merges=["A1:B1"])),
            "Inline", strings=None,
        )
        self.assertEqual(
            self._canonical(comp), self._canonical(inline)
        )
        self.assertEqual(comp.merges, inline.merges)

    def test_inline_without_shared_table(self):
        cells = {"A1": "solo"}
        sheet = parse_worksheet(
            io.BytesIO(sheet_bytes(cells, inline=True)),
            "InlineOnly", strings=None,
        )
        self.assertEqual(sheet.cells["A1"].value, "solo")


class ErrorLocationTests(unittest.TestCase):
    STRINGS = ["one", "two"]

    def _raw_sheet(self, body):
        return (
            '<?xml version="1.0"?><worksheet %s><sheetData>%s</sheetData>'
            '</worksheet>' % (NS, body)
        ).encode()

    def test_out_of_range_index(self):
        xml = self._raw_sheet('<c r="D4" t="s"><v>2</v></c>')
        with self.assertRaises(SharedStringIndexError) as ctx:
            parse_worksheet(io.BytesIO(xml), "Quarterly", strings=self.STRINGS)
        self.assertEqual(ctx.exception.sheet, "Quarterly")
        self.assertEqual(ctx.exception.coord, "D4")
        self.assertEqual(ctx.exception.index, 2)
        self.assertEqual(ctx.exception.size, 2)

    def test_negative_index(self):
        xml = self._raw_sheet('<c r="Z10" t="s"><v>-1</v></c>')
        with self.assertRaises(SharedStringIndexError) as ctx:
            parse_worksheet(io.BytesIO(xml), "NegTab", strings=self.STRINGS)
        self.assertEqual(ctx.exception.sheet, "NegTab")
        self.assertEqual(ctx.exception.coord, "Z10")
        self.assertEqual(ctx.exception.index, -1)

    def test_empty_table_any_index_rejected(self):
        xml = self._raw_sheet('<c r="A1" t="s"><v>0</v></c>')
        with self.assertRaises(SharedStringIndexError) as ctx:
            parse_worksheet(io.BytesIO(xml), "EmptySST", strings=[])
        self.assertEqual(ctx.exception.size, 0)
        self.assertEqual(ctx.exception.coord, "A1")

    def test_missing_shared_table_rejected(self):
        xml = self._raw_sheet('<c r="B2" t="s"><v>0</v></c>')
        with self.assertRaises(MissingSharedStringsError) as ctx:
            parse_worksheet(io.BytesIO(xml), "NoSST", strings=None)
        self.assertEqual(ctx.exception.sheet, "NoSST")
        self.assertEqual(ctx.exception.coord, "B2")

    def test_not_an_index(self):
        xml = self._raw_sheet('<c r="C3" t="s"><v>abc</v></c>')
        with self.assertRaises(SstError) as ctx:
            parse_worksheet(io.BytesIO(xml), "Bad", strings=self.STRINGS)
        self.assertEqual(ctx.exception.coord, "C3")

    def test_duplicate_coordinate_rejected(self):
        xml = self._raw_sheet(
            '<c r="A1" t="s"><v>0</v></c><c r="A1" t="s"><v>1</v></c>'
        )
        with self.assertRaises(SstError):
            parse_worksheet(io.BytesIO(xml), "Dup", strings=self.STRINGS)

    def test_no_blank_substitute(self):
        xml = self._raw_sheet('<c r="A1" t="s"><v>99</v></c>')
        try:
            parse_worksheet(io.BytesIO(xml), "NoBlank", strings=self.STRINGS)
        except SharedStringIndexError:
            return
        self.fail("out of range index must raise, not resolve to blank")


if __name__ == "__main__":
    unittest.main(verbosity=2)

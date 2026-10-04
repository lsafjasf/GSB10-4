"""自测: 边界用例 + 富文本顺序/格式 + 合并稀疏 + 压缩/未压缩对拍。仅标准库。"""
import random
import unittest

from shared_strings import (
    RichText, Run, SharedStringTable, Sheet,
    SharedStringError, MissingSharedStringTableError,
    SharedStringIndexError, MalformedCellError,
    build_sst, dump_sst, dump_sheet, parse_coord, format_coord,
)


def R(text, *formats):
    return Run(text, tuple((tag, tuple(sorted(attrs.items()))) for tag, attrs in formats))


def plain(text):
    return RichText((Run(text),))


SST_XML = """<?xml version="1.0"?>
<sst count="3" uniqueCount="3">
  <si><t>苹果</t></si>
  <si><r><rPr><b/></rPr><t>加粗</t></r><r><t>普通</t></r><r><rPr><i/><color rgb="FFFF0000"/></rPr><t>红色斜体</t></r></si>
  <si><t xml:space="preserve">  前后空格  </t></si>
</sst>"""


class CoordTests(unittest.TestCase):
    def test_roundtrip(self):
        for ref in ("A1", "Z9", "AA10", "XFD1048576", "A1048576"):
            self.assertEqual(format_coord(*parse_coord(ref)), ref)

    def test_invalid(self):
        for bad in ("A0", "0", "1A", "A", ""):
            with self.assertRaises(ValueError):
                parse_coord(bad)


class RichTextTests(unittest.TestCase):
    def test_runs_order_and_formats(self):
        sst = SharedStringTable.from_xml(SST_XML)
        entry = sst.lookup(1, "富文本表", "B2")
        self.assertEqual(entry.text, "加粗普通红色斜体")
        self.assertEqual([r.text for r in entry.runs], ["加粗", "普通", "红色斜体"])
        self.assertEqual(entry.format_tags(), (("b",), (), ("i", "color")))
        bold = entry.runs[0]
        self.assertEqual(bold.formats[0][0], "b")
        red = entry.runs[2]
        color = dict(dict(red.formats[1][1]) and dict(red.formats[1][1]))
        self.assertEqual(dict(red.formats[1][1])["rgb"], "FFFF0000")

    def test_whitespace_preserved(self):
        sst = SharedStringTable.from_xml(SST_XML)
        self.assertEqual(sst.lookup(2).text, "  前后空格  ")

    def test_empty_runs_normalize_to_empty_text(self):
        self.assertEqual(RichText(()).text, "")


class IndexErrorTests(unittest.TestCase):
    def setUp(self):
        self.sst = SharedStringTable.from_xml(SST_XML)

    def sheet(self, value_ref, sheet_name="价格表"):
        return (
            f'<worksheet><sheetData><row r="1">'
            f'<c r="{value_ref[0]}" t="s"><v>{value_ref[1]}</v></c>'
            f"</row></sheetData></worksheet>",
            sheet_name, value_ref[0],
        )

    def test_index_equal_to_size_rejected(self):
        xml, name, coord = self.sheet(("C7", "3"))
        with self.assertRaises(SharedStringIndexError) as ctx:
            Sheet.from_xml(xml, self.sst, name)
        self.assertEqual(ctx.exception.sheet, name)
        self.assertEqual(ctx.exception.coord, coord)
        self.assertIn("C7", str(ctx.exception))
        self.assertIn("价格表", str(ctx.exception))

    def test_index_beyond_size_rejected(self):
        xml, name, coord = self.sheet(("D8", "999"))
        with self.assertRaises(SharedStringIndexError) as ctx:
            Sheet.from_xml(xml, self.sst, name)
        self.assertEqual(ctx.exception.index, 999)
        self.assertEqual(ctx.exception.size, 3)

    def test_negative_index_rejected(self):
        xml, _n, _c = self.sheet(("A1", "-1"))
        with self.assertRaises(SharedStringIndexError) as ctx:
            Sheet.from_xml(xml, self.sst, "负索引表")
        self.assertEqual(ctx.exception.index, -1)
        self.assertIn("负索引表!A1", str(ctx.exception))

    def test_non_integer_index_rejected(self):
        xml, _n, _c = self.sheet(("A1", "abc"))
        with self.assertRaises(MalformedCellError):
            Sheet.from_xml(xml, self.sst, "坏索引表")

    def test_missing_shared_table_rejected(self):
        xml = '<worksheet><sheetData><row r="1"><c r="B5" t="s"><v>0</v></c></row></sheetData></worksheet>'
        with self.assertRaises(MissingSharedStringTableError) as ctx:
            Sheet.from_xml(xml, None, "库存表")
        self.assertEqual(ctx.exception.sheet, "库存表")
        self.assertEqual(ctx.exception.coord, "B5")
        self.assertIn("库存表!B5", str(ctx.exception))

    def test_never_returns_blank_for_bad_index(self):
        xml, _n, _c = self.sheet(("A1", "3"))
        try:
            Sheet.from_xml(xml, self.sst, "价格表")
        except SharedStringError as exc:
            self.assertNotIn(str(exc).strip(), ("", None))
            self.assertNotIn("\x00", str(exc))
        else:
            self.fail("越界索引必须抛错, 不能静默返回空白")


class EmptySSTTests(unittest.TestCase):
    EMPTY = '<sst count="0" uniqueCount="0"></sst>'

    def test_empty_sst_ok_without_shared_cells(self):
        sst = SharedStringTable.from_xml(self.EMPTY)
        self.assertEqual(len(sst), 0)
        xml = '<worksheet><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>hi</t></is></c></row></sheetData></worksheet>'
        sheet = Sheet.from_xml(xml, sst, "空表用例")
        self.assertEqual(sheet.cell_texts(), {"A1": "hi"})

    def test_empty_sst_reference_rejected(self):
        sst = SharedStringTable.from_xml(self.EMPTY)
        xml = '<worksheet><sheetData><row r="1"><c r="C3" t="s"><v>0</v></c></row></sheetData></worksheet>'
        with self.assertRaises(SharedStringIndexError) as ctx:
            Sheet.from_xml(xml, sst, "空表引用")
        self.assertEqual(ctx.exception.size, 0)
        self.assertEqual(ctx.exception.coord, "C3")


class SameTextColumnTests(unittest.TestCase):
    def test_whole_column_same_text_single_entry(self):
        sst = SharedStringTable.from_xml(SST_XML)
        rows = "".join(
            f'<row r="{r}"><c r="A{r}" t="s"><v>0</v></c></row>' for r in range(1, 51)
        )
        sheet = Sheet.from_xml(f"<worksheet><sheetData>{rows}</sheetData></worksheet>", sst, "同文本列")
        self.assertEqual(len(sheet), 50)
        texts = sheet.cell_texts()
        self.assertTrue(all(v == "苹果" for v in texts.values()))
        self.assertEqual(len(sst), 3)  # 表本身仍只有 3 条, 50 格共用 1 条


class SingleCellSheetTests(unittest.TestCase):
    def test_single_cell_sheet(self):
        sst = SharedStringTable.from_xml(SST_XML)
        xml = '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData></worksheet>'
        sheet = Sheet.from_xml(xml, sst, "单格表")
        self.assertEqual(len(sheet), 1)
        self.assertEqual(sheet.cell_texts(), {"A1": "苹果"})
        self.assertEqual(sheet.bounds, (1, 1))


class MergeAndSparseTests(unittest.TestCase):
    def test_merge_expands_only_explicit_range(self):
        sst = SharedStringTable.from_xml(SST_XML)
        xml = (
            '<worksheet><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="D1" t="s"><v>0</v></c></row>'
            '</sheetData>'
            '<mergeCells><mergeCell ref="A1:B2"/></mergeCells></worksheet>'
        )
        sheet = Sheet.from_xml(xml, sst, "合并表")
        self.assertEqual(len(sheet), 2)  # 展开前只有显式存在的 A1, D1
        sheet.expand_merges()
        self.assertEqual(sorted(sheet.cell_texts()), ["A1", "A2", "B1", "B2", "D1"])
        self.assertEqual(sheet.get(1, 2).text, "苹果")
        self.assertIsNone(sheet.get(3, 1))   # 范围外不补
        self.assertIsNone(sheet.get(1, 3))  # 范围外不补 (C1)
        self.assertIsNone(sheet.get(100, 100))

    def test_sparse_sheet_does_not_fill_region(self):
        sst = SharedStringTable.from_xml(SST_XML)
        xml = (
            '<worksheet><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'
            '<row r="9000"><c r="ZZ9000" t="s"><v>0</v></c></row>'
            '</sheetData></worksheet>'
        )
        sheet = Sheet.from_xml(xml, sst, "稀疏表")
        self.assertEqual(len(sheet), 2)  # 不随 9000x702 平方膨胀
        self.assertEqual(sheet.bounds, (9000, 702))

    def test_anchorless_merge_rejected(self):
        sst = SharedStringTable.from_xml(SST_XML)
        xml = (
            '<worksheet><sheetData><row r="1"><c r="D1" t="s"><v>0</v></c></row></sheetData>'
            '<mergeCells><mergeCell ref="A1:B2"/></mergeCells></worksheet>'
        )
        sheet = Sheet.from_xml(xml, sst, "坏合并")
        with self.assertRaises(SharedStringError):
            sheet.expand_merges()


class DifferentialTests(unittest.TestCase):
    """压缩存储 vs 未压缩存储逐单元格对拍 (小样本, 大样本由 gen_diff_data.py 生成)。"""

    def _random_workbook(self, rng, n_cells):
        formats_pool = [
            ("b", []), ("i", []), ("u", []),
            ("sz", [("val", "11")]), ("color", [("rgb", "FFFF0000")]),
            ("rFont", [("val", "Arial")]),
        ]
        words = ["苹果", "banana", "库存/数量", "  pad  ", "中文😀emoji", "x" * 30, ""]

        def make_rich():
            n_runs = rng.randint(1, 4)
            runs = []
            for _ in range(n_runs):
                fmts = tuple((t, tuple(a)) for t, a in rng.sample(formats_pool, rng.randint(0, 2)))
                runs.append(Run(rng.choice(words), fmts))
            return RichText(tuple(runs))

        cells = {}
        while len(cells) < n_cells:
            r = rng.randint(1, 400)
            c = rng.randint(1, 60)
            cells[(r, c)] = make_rich()
        merges = []
        coords = sorted(cells)
        for anchor in rng.sample(coords, min(3, len(coords))):
            r, c = anchor
            merges.append((r, c, r + rng.randint(0, 2), c + rng.randint(0, 2)))
        return cells, merges

    def test_compressed_matches_uncompressed_cell_by_cell(self):
        rng = random.Random(20261005)
        for sheet_name in ("sheet_a", "订单表"):
            cells, merges = self._random_workbook(rng, 400)
            entries, index = build_sst(cells.values())
            compressed = Sheet.from_xml(dump_sheet(cells, merges, index),
                                        SharedStringTable.from_xml(dump_sst(entries)), sheet_name)
            uncompressed = Sheet.from_xml(dump_sheet(cells, merges, None), None, sheet_name)
            compressed.expand_merges()
            uncompressed.expand_merges()
            self.assertEqual(set(compressed.cells), set(uncompressed.cells))
            self.assertEqual(len(compressed.cells), len(uncompressed.cells))
            for key in compressed.cells:
                a, b = compressed.cells[key], uncompressed.cells[key]
                self.assertEqual(a, b, f"{sheet_name}!{format_coord(*key)} 对拍不一致")
                self.assertEqual(a.text, b.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

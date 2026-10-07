"""Generate cross-check fixtures and edge-case packages under data/."""

import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sstlib import RichText, build_xlsx
from sstlib.builder import (
    CONTENT_TYPES,
    ROOT_RELS,
    WORKBOOK,
    WORKBOOK_RELS,
    shared_strings_xml,
)

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def write_pkg(path, sheet_body, sst_xml=None, sheet_name="Sheet1",
              merges=""):
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        "<worksheet %s><sheetData>%s</sheetData>%s</worksheet>"
        % (NS, sheet_body, merges)
    )
    workbook = WORKBOOK.replace("{name}", '"%s"' % sheet_name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
        if sst_xml is not None:
            zf.writestr("xl/sharedStrings.xml", sst_xml)


def canonical(value):
    if isinstance(value, RichText):
        return {"runs": [[text, props] for text, props in value.runs]}
    return value


def make_crosscheck():
    rich = RichText([
        ("总计", {"b": {}, "sz": {"val": "14"}}),
        ("（", {}),
        ("含税", {"i": {}, "color": {"rgb": "FF0070C0"}}),
        ("）", {}),
    ])
    strings = [
        "alpha", "beta", "alpha",  # duplicate kept: table is positional
        "  前后空格  ", "整列同文本", rich, "末行", "unicode 表格 🎉",
    ]
    cells = {
        "A1": "alpha", "B1": "beta", "C1": "alpha",
        "A2": None, "B2": 42, "C2": 3.14159,
        "A3": True, "B3": False,
        "A4": "  前后空格  ",
        "B4": rich,
        "C4": "unicode 表格 🎉",
        "D7": "末行",
    }
    # a whole column sharing one text
    for row in range(10, 60):
        cells["E%d" % row] = "整列同文本"
    merges = ["A6:C6", "B8:B9"]

    expected = {}
    for coord, value in cells.items():
        if value is None:
            expected[coord] = {"kind": "empty", "value": None}
        elif isinstance(value, bool):
            expected[coord] = {"kind": "bool",
                               "value": "1" if value else "0"}
        elif isinstance(value, (int, float)):
            expected[coord] = {"kind": "number", "value": repr(value)}
        else:
            expected[coord] = {"kind": "str", "value": canonical(value)}

    build_xlsx(
        os.path.join(DATA, "crosscheck_compressed.xlsx"),
        cells, compressed=True, strings=strings, merges=merges,
        sheet_name="对拍表", dimension="A1:E59",
    )
    build_xlsx(
        os.path.join(DATA, "crosscheck_inline.xlsx"),
        cells, compressed=False, merges=merges,
        sheet_name="对拍表", dimension="A1:E59",
    )
    with open(os.path.join(DATA, "crosscheck_expected.json"), "w",
              encoding="utf-8") as fh:
        json.dump(
            {"sheet": "对拍表", "merges": merges, "cells": expected},
            fh, ensure_ascii=False, indent=1, sort_keys=True,
        )


def make_edge_cases():
    # empty shared table, no string cells -> must parse fine
    write_pkg(
        os.path.join(DATA, "edge_empty_sst_ok.xlsx"),
        '<c r="A1"><v>1</v></c><c r="B2"><v>2.5</v></c>',
        sst_xml=shared_strings_xml([]),
        sheet_name="空共享表",
    )
    # empty shared table but a cell indexes into it -> must be rejected
    write_pkg(
        os.path.join(DATA, "edge_empty_sst_bad.xlsx"),
        '<c r="C3" t="s"><v>0</v></c>',
        sst_xml=shared_strings_xml([]),
        sheet_name="空表越界",
    )
    # no sharedStrings part at all, cell wants it -> must be rejected
    write_pkg(
        os.path.join(DATA, "edge_missing_sst.xlsx"),
        '<c r="B2" t="s"><v>0</v></c>',
        sst_xml=None,
        sheet_name="缺共享表",
    )
    # index past the end of the table
    write_pkg(
        os.path.join(DATA, "edge_oor_index.xlsx"),
        '<c r="A1" t="s"><v>0</v></c><c r="D4" t="s"><v>7</v></c>',
        sst_xml=shared_strings_xml(["only"]),
        sheet_name="越界表",
    )
    # negative index
    write_pkg(
        os.path.join(DATA, "edge_negative_index.xlsx"),
        '<c r="Z10" t="s"><v>-2</v></c>',
        sst_xml=shared_strings_xml(["a", "b"]),
        sheet_name="负索引表",
    )
    # whole column of identical text (1000 rows, one shared entry)
    body = "".join(
        '<c r="A%d" t="s"><v>0</v></c>' % row for row in range(1, 1001)
    )
    write_pkg(
        os.path.join(DATA, "edge_whole_column.xlsx"),
        body,
        sst_xml=shared_strings_xml(["整列同文本"]),
        sheet_name="整列同文本",
    )
    # single cell sheet
    write_pkg(
        os.path.join(DATA, "edge_single_cell.xlsx"),
        '<c r="E5" t="s"><v>0</v></c>',
        sst_xml=shared_strings_xml(["孤"]),
        sheet_name="单格表",
    )
    # sparse coordinates + explicit merges; huge declared dimension
    write_pkg(
        os.path.join(DATA, "edge_sparse_merged.xlsx"),
        '<c r="A1" t="s"><v>0</v></c>'
        '<c r="ZZ100000" t="s"><v>1</v></c>'
        '<c r="B2"><v>9</v></c>',
        sst_xml=shared_strings_xml(["头", "尾"]),
        sheet_name="稀疏合并",
        merges='<mergeCells count="2">'
               '<mergeCell ref="C3:D4"/><mergeCell ref="F1:F3"/>'
               "</mergeCells>",
    )


def main():
    os.makedirs(DATA, exist_ok=True)
    make_crosscheck()
    make_edge_cases()
    for name in sorted(os.listdir(DATA)):
        print("wrote data/%s" % name)


if __name__ == "__main__":
    main()

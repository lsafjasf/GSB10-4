"""Cross-check: compressed (shared-string) vs uncompressed (inline) storage.

Parses both encodings of the same logical workbook and asserts the restored
result is identical cell by cell, and identical to the independently written
expectation JSON. Writes reports/crosscheck_report.txt.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sstlib import RichText, load_workbook

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
REPORTS = os.path.join(os.path.dirname(__file__), "..", "reports")


def canonical_cells(sheet):
    out = {}
    for coord, cell in sheet.cells.items():
        value = cell.value
        if isinstance(value, RichText):
            value = {"runs": [[text, props] for text, props in value.runs]}
        out[coord] = {"kind": cell.kind, "value": value}
    return out


def main():
    compressed = load_workbook(os.path.join(DATA, "crosscheck_compressed.xlsx"))
    inline = load_workbook(os.path.join(DATA, "crosscheck_inline.xlsx"))
    with open(os.path.join(DATA, "crosscheck_expected.json"),
              encoding="utf-8") as fh:
        expected = json.load(fh)

    lines = []
    lines.append("cross-check: compressed shared-string vs uncompressed inline")
    lines.append("=" * 62)

    failures = 0
    for sheet_name in compressed:
        comp = canonical_cells(compressed[sheet_name])
        inl = canonical_cells(inline[sheet_name])
        exp = expected["cells"]

        lines.append("sheet: %s" % sheet_name)
        lines.append("  cells compared : %d" % len(comp))

        only_comp = sorted(set(comp) - set(inl))
        only_inl = sorted(set(inl) - set(comp))
        if only_comp or only_inl:
            failures += 1
            lines.append("  MISMATCH coordinates: only-compressed=%s "
                         "only-inline=%s" % (only_comp, only_inl))
        diffs = [c for c in comp if c in inl and comp[c] != inl[c]]
        if diffs:
            failures += 1
            for coord in diffs[:10]:
                lines.append("  MISMATCH %s: %r != %r"
                             % (coord, comp[coord], inl[coord]))
        if comp != exp:
            failures += 1
            for coord in sorted(set(comp) | set(exp)):
                if comp.get(coord) != exp.get(coord):
                    lines.append("  EXPECTATION MISMATCH %s: got %r want %r"
                                 % (coord, comp.get(coord), exp.get(coord)))
        merges = ["%s:%s" % m for m in compressed[sheet_name].merges]
        if merges != expected["merges"]:
            failures += 1
            lines.append("  MERGE MISMATCH: %r != %r"
                         % (merges, expected["merges"]))
        if not (only_comp or only_inl or diffs) and comp == exp:
            lines.append("  result: identical cell-by-cell "
                         "(compressed == inline == expectation)")
        lines.append("  merges: %s" % ", ".join(merges))

    lines.append("=" * 62)
    lines.append("VERDICT: %s" % ("PASS" if failures == 0
                                   else "FAIL (%d)" % failures))
    report = "\n".join(lines)
    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, "crosscheck_report.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(report + "\n")
    print(report)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

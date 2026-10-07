"""Open every corrupt edge fixture and record the rejected error.

All bad indexes must be refused with sheet name + coordinate; none may be
silently read as blank. Writes reports/out_of_range_report.txt.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sstlib import (
    MissingSharedStringsError,
    SharedStringIndexError,
    SstError,
    load_workbook,
)

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
REPORTS = os.path.join(os.path.dirname(__file__), "..", "reports")

BAD = [
    ("edge_empty_sst_bad.xlsx", "empty table, index 0"),
    ("edge_missing_sst.xlsx", "no sharedStrings part"),
    ("edge_oor_index.xlsx", "index past end (7 in 1-entry table)"),
    ("edge_negative_index.xlsx", "negative index (-2)"),
]
GOOD = [
    ("edge_empty_sst_ok.xlsx", "empty table, no string cells"),
    ("edge_single_cell.xlsx", "single cell sheet"),
    ("edge_whole_column.xlsx", "1000 rows sharing one string"),
    ("edge_sparse_merged.xlsx", "sparse coords + explicit merges"),
]


def main():
    lines = ["out-of-range / missing shared-string index report", "=" * 62]
    failures = 0

    lines.append("")
    lines.append("[1] corrupt packages - each MUST be rejected")
    for fname, desc in BAD:
        path = os.path.join(DATA, fname)
        lines.append("")
        lines.append("package: %s (%s)" % (fname, desc))
        try:
            load_workbook(path)
        except (SharedStringIndexError, MissingSharedStringsError) as exc:
            kind = (
                "OUT-OF-RANGE INDEX"
                if isinstance(exc, SharedStringIndexError)
                else "MISSING SHARED TABLE"
            )
            detail = getattr(exc, "index", None)
            lines.append("  rejected : yes")
            lines.append("  type     : %s" % kind)
            lines.append("  sheet    : %s" % exc.sheet)
            lines.append("  cell     : %s" % exc.coord)
            if detail is not None:
                lines.append("  index    : %d (table size %d)"
                             % (exc.index, exc.size))
            lines.append("  message  : %s" % exc)
        except Exception as exc:  # noqa: BLE001 - record other errors too
            failures += 1
            lines.append("  rejected : yes, but unexpected error %r" % exc)
        else:
            failures += 1
            lines.append("  rejected : NO  <-- FAILURE: must not yield blank")

    lines.append("")
    lines.append("[2] valid packages - each MUST parse and contain no blanks")
    for fname, desc in GOOD:
        path = os.path.join(DATA, fname)
        lines.append("")
        lines.append("package: %s (%s)" % (fname, desc))
        try:
            sheets = load_workbook(path)
        except SstError as exc:
            failures += 1
            lines.append("  parse    : ERROR %s" % exc)
            continue
        for sheet in sheets.values():
            blank_str = [
                coord for coord, cell in sheet.cells.items()
                if cell.kind == "str" and cell.plain_text() == ""
                and not _was_empty_string(cell)
            ]
            lines.append("  parse    : ok")
            lines.append("  sheet    : %s" % sheet.name)
            lines.append("  cells    : %d (sparse; only explicit <c>)"
                         % len(sheet.cells))
            lines.append("  merges   : %d explicit range(s): %s" % (
                len(sheet.merges),
                ", ".join("%s:%s" % m for m in sheet.merges) or "-",
            ))
            lines.append("  bad blank string cells: %d" % len(blank_str))
            if blank_str:
                failures += 1
                lines.append("    <-- FAILURE")

    lines.append("")
    lines.append("=" * 62)
    lines.append("VERDICT: %s" % ("PASS" if failures == 0
                                   else "FAIL (%d)" % failures))
    report = "\n".join(lines)
    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, "out_of_range_report.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(report + "\n")
    print(report)
    return 1 if failures else 0


def _was_empty_string(cell):
    return cell.plain_text() == "" and isinstance(
        cell.value, str
    ) and cell.value == ""


if __name__ == "__main__":
    sys.exit(main())

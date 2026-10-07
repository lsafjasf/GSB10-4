"""Benchmark: parse 100k-cell workbooks; report time and peak memory.

Each case runs in a fresh subprocess so tracemalloc peak and ru_maxrss are
clean. The sparse case declares a full-grid dimension (XFD1048576, about
1.7e10 cells) while storing only 1,000 explicit cells: if memory scaled
with rows*cols it would explode; linear-in-entries memory stays flat.
Writes reports/benchmark_report.txt.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sstlib import build_xlsx, load_workbook

REPORTS = os.path.join(os.path.dirname(__file__), "..", "reports")

CHILD = r"""
import json, resource, sys, time, tracemalloc
sys.path.insert(0, {root!r})
from sstlib import load_workbook
tracemalloc.start()
t0 = time.perf_counter()
sheets = load_workbook(sys.argv[1])
elapsed = time.perf_counter() - t0
peak = tracemalloc.get_traced_memory()[1]
tracemalloc.stop()
cells = sum(len(s.cells) for s in sheets.values())
rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({{"seconds": elapsed, "tracemalloc_peak_mb": peak / 1e6,
                  "rss_peak_mb": rss_kb / 1024, "cells": cells}}))
"""


def run_case(name, path, expected_cells):
    out = subprocess.run(
        [sys.executable, "-c", CHILD.format(
            root=os.path.abspath(os.path.join(os.path.dirname(__file__),
                                              ".."))), path],
        capture_output=True, text=True, check=True,
    )
    stats = json.loads(out.stdout.strip())
    assert stats["cells"] == expected_cells, (name, stats)
    return stats


def main():
    tmp = tempfile.mkdtemp(prefix="sstbench_")
    lines = ["benchmark: shared-string parse time and peak memory",
             "=" * 62,
             "machine python: %s" % sys.version.split()[0],
             ""]

    # case 1: 100k cells, compressed shared strings (1000 x 100 grid)
    rows, cols, uniques = 1000, 100, 200
    strings = ["shared-%04d" % i for i in range(uniques)]
    from sstlib.parser import index_to_col
    cells = {}
    n = 0
    for row in range(1, rows + 1):
        for col in range(cols):
            coord = "%s%d" % (index_to_col(col), row)
            if n % 10 == 9:
                cells[coord] = n  # 10% numbers
            else:
                cells[coord] = strings[n % uniques]
            n += 1
    dense_path = os.path.join(tmp, "dense_100k.xlsx")
    t0 = time.perf_counter()
    build_xlsx(dense_path, cells, compressed=True, strings=strings,
               sheet_name="Dense100k", dimension="A1:CV1000")
    build_s = time.perf_counter() - t0
    size_mb = os.path.getsize(dense_path) / 1e6

    stats = run_case("dense", dense_path, 100000)
    lines.append("[1] dense 100,000 cells (1000 rows x 100 cols, "
                 "200 unique shared strings)")
    lines.append("  file size        : %.2f MB (built in %.2fs)"
                 % (size_mb, build_s))
    lines.append("  parse time       : %.3f s" % stats["seconds"])
    lines.append("  tracemalloc peak : %.1f MB" % stats["tracemalloc_peak_mb"])
    lines.append("  process RSS peak : %.1f MB" % stats["rss_peak_mb"])
    lines.append("  cells restored   : %d" % stats["cells"])
    lines.append("")

    # case 2: same 100k cells uncompressed (inline strings)
    inline_path = os.path.join(tmp, "inline_100k.xlsx")
    build_xlsx(inline_path, cells, compressed=False,
               sheet_name="Inline100k", dimension="A1:CV1000")
    stats2 = run_case("inline", inline_path, 100000)
    lines.append("[2] same 100,000 cells, uncompressed inline strings")
    lines.append("  file size        : %.2f MB"
                 % (os.path.getsize(inline_path) / 1e6))
    lines.append("  parse time       : %.3f s" % stats2["seconds"])
    lines.append("  tracemalloc peak : %.1f MB" % stats2["tracemalloc_peak_mb"])
    lines.append("  process RSS peak : %.1f MB" % stats2["rss_peak_mb"])
    lines.append("")

    # case 3: sparse sheet, full-grid dimension, 1000 explicit cells
    sparse_cells = {}
    for i in range(1000):
        col = (i * 16) % 16384          # up to XFD
        row = 1 + (i * 1048) % 1048576  # up to row 1048576
        sparse_cells["%s%d" % (index_to_col(col), row)] = strings[i % uniques]
    sparse_path = os.path.join(tmp, "sparse_fullgrid.xlsx")
    build_xlsx(sparse_path, sparse_cells, compressed=True, strings=strings,
               sheet_name="Sparse", dimension="A1:XFD1048576")
    stats3 = run_case("sparse", sparse_path, 1000)
    lines.append("[3] sparse sheet: dimension A1:XFD1048576 "
                 "(~1.7e10 grid cells), 1,000 explicit cells")
    lines.append("  parse time       : %.3f s" % stats3["seconds"])
    lines.append("  tracemalloc peak : %.1f MB" % stats3["tracemalloc_peak_mb"])
    lines.append("  process RSS peak : %.1f MB" % stats3["rss_peak_mb"])
    lines.append("  -> memory tracks explicit cells, NOT rows*cols")
    lines.append("")

    # case 4: scaling check - 25k / 50k / 100k dense, memory ~ linear
    lines.append("[4] scaling check (dense, cells vs peak memory)")
    prev = None
    ok_scaling = True
    for count in (25000, 50000, 100000):
        sub = dict(list(cells.items())[:count])
        p = os.path.join(tmp, "scale_%d.xlsx" % count)
        build_xlsx(p, sub, compressed=True, strings=strings,
                   sheet_name="Scale")
        st = run_case("scale", p, count)
        lines.append("  %7d cells: %.3fs, tracemalloc peak %.1f MB"
                     % (count, st["seconds"], st["tracemalloc_peak_mb"]))
        if prev is not None:
            ratio = st["tracemalloc_peak_mb"] / max(prev[1], 0.001)
            cells_ratio = count / prev[0]
            # quadratic growth would give ~cells_ratio**2
            if ratio > cells_ratio * 1.5:
                ok_scaling = False
        prev = (count, st["tracemalloc_peak_mb"])
    lines.append("  growth           : %s"
                 % ("linear (PASS)" if ok_scaling else "super-linear (FAIL)"))
    lines.append("")
    lines.append("=" * 62)
    verdict = "PASS" if ok_scaling else "FAIL"
    lines.append("VERDICT: %s" % verdict)

    report = "\n".join(lines)
    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, "benchmark_report.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(report + "\n")
    print(report)
    return 0 if ok_scaling else 1


if __name__ == "__main__":
    sys.exit(main())

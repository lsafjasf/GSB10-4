"""生成压缩/未压缩两套对拍数据, 并逐单元格比对, 输出 data/diff/diff_report.txt。"""
import os
import random

from shared_strings import (
    RichText, Run, SharedStringTable, Sheet,
    build_sst, dump_sst, dump_sheet, format_coord,
)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "diff")

SHEETS = ("订单", "库存", "日志")
CELLS_PER_SHEET = 1500


def random_rich(rng, formats_pool, words):
    runs = []
    for _ in range(rng.randint(1, 4)):
        fmts = tuple((t, tuple(a)) for t, a in rng.sample(formats_pool, rng.randint(0, 2)))
        runs.append(Run(rng.choice(words), fmts))
    return RichText(tuple(runs))


def main():
    rng = random.Random(20261005)
    formats_pool = [
        ("b", []), ("i", []), ("u", []),
        ("sz", [("val", "11")]), ("sz", [("val", "14")]),
        ("color", [("rgb", "FFFF0000")]), ("color", [("rgb", "FF008000")]),
        ("rFont", [("val", "Arial")]), ("rFont", [("val", "宋体")]),
    ]
    words = [
        "苹果", "香蕉", "banana", "库存/数量", "  前后空格  ", "中文😀emoji",
        "SKU-0001", "单价 ¥12.50", "x" * 40, "", "换行\n文本", "制表\t符",
    ]

    workbooks = {}
    for name in SHEETS:
        cells = {}
        while len(cells) < CELLS_PER_SHEET:
            cells[(rng.randint(1, 5000), rng.randint(1, 200))] = random_rich(rng, formats_pool, words)
        merges = []
        for anchor in rng.sample(sorted(cells), 8):
            r, c = anchor
            merges.append((r, c, r + rng.randint(0, 3), c + rng.randint(0, 3)))
        workbooks[name] = (cells, merges)

    all_values = [v for cells, _m in workbooks.values() for v in cells.values()]
    entries, index = build_sst(all_values)

    comp_dir = os.path.join(OUT, "compressed")
    uncomp_dir = os.path.join(OUT, "uncompressed")
    os.makedirs(comp_dir, exist_ok=True)
    os.makedirs(uncomp_dir, exist_ok=True)

    with open(os.path.join(comp_dir, "sharedStrings.xml"), "w", encoding="utf-8") as fh:
        fh.write(dump_sst(entries))
    for name, (cells, merges) in workbooks.items():
        with open(os.path.join(comp_dir, f"{name}.xml"), "w", encoding="utf-8") as fh:
            fh.write(dump_sheet(cells, merges, index))
        with open(os.path.join(uncomp_dir, f"{name}.xml"), "w", encoding="utf-8") as fh:
            fh.write(dump_sheet(cells, merges, None))

    sst = SharedStringTable.from_xml(os.path.join(comp_dir, "sharedStrings.xml"))
    lines = [
        "对拍报告: 压缩存储 (SST+索引) vs 未压缩存储 (inlineStr)",
        "=" * 60,
        f"共享表条目数 (uniqueCount): {len(sst)}",
        "",
    ]
    total = mismatches = 0
    for name in SHEETS:
        comp = Sheet.from_xml(os.path.join(comp_dir, f"{name}.xml"), sst, name)
        uncomp = Sheet.from_xml(os.path.join(uncomp_dir, f"{name}.xml"), None, name)
        comp.expand_merges()
        uncomp.expand_merges()
        sheet_mismatch = 0
        keys = set(comp.cells) | set(uncomp.cells)
        for key in sorted(keys):
            total += 1
            a, b = comp.cells.get(key), uncomp.cells.get(key)
            if a != b:
                mismatches += 1
                sheet_mismatch += 1
                lines.append(
                    f"MISMATCH {name}!{format_coord(*key)}: 压缩={a!r} 未压缩={b!r}"
                )
        lines.append(
            f"[{name}] 单元格数(含合并展开): {len(keys)}, 不一致: {sheet_mismatch}"
        )
    lines.append("")
    lines.append(f"总比对单元格: {total}, 不一致: {mismatches}")
    lines.append("结果: " + ("PASS (逐单元格完全一致)" if mismatches == 0 else "FAIL"))
    report = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "diff_report.txt"), "w", encoding="utf-8") as fh:
        fh.write(report)
    print(report)


if __name__ == "__main__":
    main()

"""演示入口：展开演示文档，产出全部交付物到 out/ 目录。

运行: python3 demo.py
"""
import json
import os
import sys

from style_inherit import expand, write_char_table_tsv
from style_inherit.cases import demo_document
from manual_reference import diff_expand, manual_expand

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    doc = demo_document()
    expanded = expand(doc)

    # 1) 逐字符属性表（TSV，每字符一行，每属性 值/来源 两列）
    with open(os.path.join(OUT, "char_table.tsv"), "w", encoding="utf-8") as fh:
        write_char_table_tsv(expanded, fh)

    # 2) 逐字符冲突候选明细（JSONL，含每层候选与胜出标记）
    n_rows = 0
    with open(os.path.join(OUT, "char_detail.jsonl"), "w",
              encoding="utf-8") as fh:
        for para in expanded.paragraphs:
            char_index = 0
            for run in para.runs:
                explained = expanded.explain(para.index, run.run_index)
                for offset, ch in enumerate(run.text):
                    record = {
                        "para": para.index, "char_index": char_index,
                        "run": run.run_index, "offset_in_run": offset,
                        "char": ch, "codepoint": "U+%04X" % ord(ch),
                        "props": explained,
                    }
                    fh.write(json.dumps(record, ensure_ascii=False,
                                        sort_keys=True) + "\n")
                    char_index += 1
                    n_rows += 1

    # 3) 诊断报告（断链 / 缺失样式 / 未解析属性 / 环）
    with open(os.path.join(OUT, "diagnostics.txt"), "w",
              encoding="utf-8") as fh:
        if not expanded.diagnostics:
            fh.write("（无诊断）\n")
        for diag in expanded.diagnostics:
            fh.write("[%s] %s\n" % (diag.kind, diag.message))

    # 4) 循环引用报告（环上样式名 + 受影响段落 + 处理规则）
    cycles = [d for d in expanded.diagnostics if d.kind == "cycle"]
    with open(os.path.join(OUT, "cycle_report.txt"), "w",
              encoding="utf-8") as fh:
        if not cycles:
            fh.write("未检测到循环引用。\n")
        for num, diag in enumerate(cycles, 1):
            ring = list(diag.cycle)
            fh.write("环 #%d: %s\n" % (num, " -> ".join(ring + [ring[0]])))
            fh.write("  环上样式: %s\n" % ", ".join(ring))
            if diag.prefix:
                fh.write("  环外前驱: %s\n" % ", ".join(diag.prefix))
            affected = [p.index for p in expanded.paragraphs
                        if p.style in ring]
            fh.write("  直接使用环上样式的段落: %s\n" % (affected or "无"))
            fh.write("  处理规则: 每个环上样式生效一次，最远者优先"
                     "（环入口最近），未覆盖属性回退显式默认值。\n")

    # 5) 对拍数据：库展开 vs 逐层手工推算
    problems = diff_expand(doc)
    manual_rows, _ = manual_expand(doc)
    diff = {
        "case": "demo_document",
        "library_rows": n_rows,
        "manual_rows": len(manual_rows),
        "row_mismatches": [p for p in problems if not p.startswith("诊断")],
        "diagnostic_mismatches": [p for p in problems if p.startswith("诊断")],
        "verdict": "IDENTICAL" if not problems else "MISMATCH",
    }
    with open(os.path.join(OUT, "diff_check.json"), "w",
              encoding="utf-8") as fh:
        json.dump(diff, fh, ensure_ascii=False, indent=2)

    print("展开完成: %d 段落, %d 字符行, %d 条诊断"
          % (len(expanded.paragraphs), n_rows, len(expanded.diagnostics)))
    print("对拍结果: %s（库 %d 行 vs 手工 %d 行）"
          % (diff["verdict"], n_rows, len(manual_rows)))
    print("输出目录: %s" % OUT)
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())

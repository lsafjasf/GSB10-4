"""生成对拍数据：随机(确定性种子)文档 -> 快速实现与手工实现逐字符比对。

产出（out/ 目录）：
  char_table.tsv   快速实现的逐字符属性表 (para, idx, char, attr, value, source)
  manual_table.tsv 逐层手工推算的逐字符属性表
  diff_report.txt  两表逐字符对拍结果
  cycle_report.txt 断裂 / 循环引用诊断
"""

import difflib
import os
import random
import sys

from style_expand import StyleSheet, expand_document
from manual_expand import manual_expand

OUT = os.path.join(os.path.dirname(__file__), "out")

FONTS = ["serif", "sans", "mono"]
COLORS = ["black", "blue", "red", "green"]


def build_styles():
    rng = random.Random(20261005)
    styles = {
        "Normal": {"props": {"font": "serif", "size": 12, "bold": False,
                             "italic": False, "color": "black"}, "based_on": None},
        "Empty":  {"props": {}, "based_on": None},
        "GhostUser": {"props": {"color": "white"}, "based_on": "Missing"},
        "LoopA":  {"props": {"bold": True}, "based_on": "LoopB"},
        "LoopB":  {"props": {"size": 9}, "based_on": "LoopC"},
        "LoopC":  {"props": {"color": "pink"}, "based_on": "LoopA"},
    }
    names = ["Normal"]
    for i in range(40):
        name = f"S{i}"
        parent = names[-1] if (names and rng.random() < 0.85) else None
        props = {}
        if rng.random() < 0.6:
            props["font"] = rng.choice(FONTS)
        if rng.random() < 0.6:
            props["size"] = rng.choice([10, 11, 12, 14, 16, 18])
        if rng.random() < 0.3:
            props["bold"] = True
        if rng.random() < 0.2:
            props["italic"] = True
        if rng.random() < 0.5:
            props["color"] = rng.choice(COLORS)
        styles[name] = {"props": props, "based_on": parent}
        names.append(name)
    return styles, names


def build_doc(styles, names):
    rng = random.Random(424242)
    style_pool = names + ["Normal", "Empty", "GhostUser", "LoopA", "NoSuchStyle", None]
    doc = []
    words = "the quick brown fox jumps over lazy dog 样式继承展开".split()
    for p in range(300):
        text = " ".join(rng.choices(words, k=rng.randint(2, 8)))
        para = {"style": rng.choice(style_pool), "props": {}, "text": text, "spans": []}
        if rng.random() < 0.5:
            if rng.random() < 0.5:
                para["props"]["size"] = rng.choice([13, 15, 20])
            if rng.random() < 0.4:
                para["props"]["bold"] = True
            if rng.random() < 0.4:
                para["props"]["color"] = rng.choice(COLORS)
        for _ in range(rng.randint(0, 3)):
            if len(text) <= 1:
                break
            s = rng.randrange(len(text) - 1)
            e = rng.randrange(s + 1, len(text) + 1)
            sp = {}
            if rng.random() < 0.7:
                sp["font"] = rng.choice(FONTS)
            if rng.random() < 0.7:
                sp["size"] = rng.choice([8, 22, 24, 36])
            if rng.random() < 0.5:
                sp["italic"] = True
            if rng.random() < 0.5:
                sp["color"] = rng.choice(COLORS)
            para["spans"].append((s, e, sp))
        doc.append(para)
    return doc


def render_table(table):
    lines = []
    for pidx, para_rows in enumerate(table):
        for ci, attrs in enumerate(para_rows):
            ch = None  # 字符本身单独一列
            for prop in sorted(attrs):
                value, source = attrs[prop]
                lines.append(f"{pidx}\t{ci}\t{prop}\t{value!r}\t{source}")
    return lines


def main():
    os.makedirs(OUT, exist_ok=True)
    styles, names = build_styles()
    doc = build_doc(styles, names)

    sheet = StyleSheet({k: dict(v) for k, v in styles.items()})
    fast = expand_document(sheet, doc)
    slow, slow_diag = manual_expand(
        {k: {"props": dict(v["props"]), "based_on": v["based_on"]} for k, v in styles.items()},
        doc,
    )

    fast_lines = render_table(fast)
    slow_lines = render_table(slow)

    with open(os.path.join(OUT, "char_table.tsv"), "w", encoding="utf-8") as f:
        f.write("para\tidx\tattr\tvalue\tsource\n")
        f.write("\n".join(fast_lines) + "\n")
    with open(os.path.join(OUT, "manual_table.tsv"), "w", encoding="utf-8") as f:
        f.write("para\tidx\tattr\tvalue\tsource\n")
        f.write("\n".join(slow_lines) + "\n")

    total = len(fast_lines)
    mismatch = [l for l in difflib.unified_diff(slow_lines, fast_lines,
                                                lineterm="", n=0)]
    with open(os.path.join(OUT, "diff_report.txt"), "w", encoding="utf-8") as f:
        f.write(f"段落数: {len(doc)}\n")
        f.write(f"字符 x 属性 总记录数: {total}\n")
        f.write(f"不一致记录数: {len(mismatch)//2 if mismatch else 0}\n")
        if mismatch:
            f.write("\n".join(mismatch[:200]) + "\n")
        else:
            f.write("对拍结论: 快速实现与逐层手工推算逐字符完全一致。\n")

    diag = sorted(set(sheet.diagnostics))
    with open(os.path.join(OUT, "cycle_report.txt"), "w", encoding="utf-8") as f:
        f.write("== 断裂 / 循环引用诊断（快速实现） ==\n")
        for d in diag:
            f.write(d + "\n")
        f.write("\n== 手工实现诊断 ==\n")
        for d in sorted(set(slow_diag)):
            f.write(d + "\n")

    print(f"记录数={total} 不一致={len(mismatch)//2 if mismatch else 0}")
    print(f"诊断 {len(diag)} 条:")
    for d in diag:
        print("  " + d)
    return 1 if mismatch else 0


if __name__ == "__main__":
    sys.exit(main())

"""精度报告：枚举所有类型组合，标注是否可能丢精度，并实测丢精度用例。

运行：python脚本 scripts/precision_report.py
产物：reports/precision_report.md
"""

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from typeunify import union

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "reports", "precision_report.md")

# (组合, 是否可能丢精度, 说明)
COMBO_TABLE = [
    ("BOOL → INT",     "否", "True/False → 1/0，精确"),
    ("BOOL → FLOAT",   "否", "1/0 在 float64 中精确"),
    ("BOOL → DECIMAL", "否", "精确"),
    ("INT → FLOAT",    "是", "\\|int\\| > 2**53 时 float64 无法精确表示"),
    ("INT → DECIMAL",  "否", "Decimal 任意精度，精确"),
    ("FLOAT → DECIMAL 组合", "是", "统一类型为 FLOAT，Decimal 端可能丢精度"),
    ("DECIMAL → FLOAT", "是", "十进制小数大多无法精确表示为二进制浮点"),
]

LOSSY_CASES = [
    ("INT → FLOAT", [2 ** 53 + 1], [0.5]),
    ("INT → FLOAT", [2 ** 60 + 12345], [1.0]),
    ("DECIMAL → FLOAT", [Decimal("0.1")], [1.0]),
    ("DECIMAL → FLOAT", [Decimal("1.1")], [2.0]),
]
LOSSLESS_CASES = [
    ("INT → FLOAT（小整数）", [42], [0.5]),
    ("DECIMAL → FLOAT（可精确表示）", [Decimal("0.5")], [1.0]),
    ("INT → DECIMAL（大整数）", [2 ** 100], [Decimal("1.5")]),
    ("BOOL → INT", [True, False], [2]),
]


def run(cols):
    r = union(*cols)
    return r


def main():
    lines = [
        "# 精度丢失报告",
        "",
        "## 可能丢精度的类型组合",
        "",
        "| 组合 | 是否可能丢精度 | 说明 |",
        "| --- | --- | --- |",
    ]
    for combo, risky, note in COMBO_TABLE:
        lines.append("| %s | %s | %s |" % (combo, risky, note))

    lines += [
        "",
        "## 实测：丢精度用例的处理结果",
        "",
        "策略：不静默、不报错中断——值照常转换，事件计入 `PrecisionReport`",
        "（全量计数 + 采样，含集合号、行号、原值、来源/目标类型）。",
        "",
        "| 用例 | 输入 | 统一类型 | 转换结果 | 精度报告 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, a, b in LOSSY_CASES:
        r = run([a, b])
        ev = r.precision.samples[0]
        lines.append("| %s | %r, %r | %s | %s | %s（位置 collection %d, row %d）|" % (
            name, a, b, r.unified_type.name,
            ", ".join(repr(v) for v in r.values),
            r.precision.summary(), ev.collection, ev.row))

    lines += [
        "",
        "## 实测：不丢精度的对照用例",
        "",
        "| 用例 | 输入 | 统一类型 | 精度报告 |",
        "| --- | --- | --- | --- |",
    ]
    for name, a, b in LOSSLESS_CASES:
        r = run([a, b])
        lines.append("| %s | %r, %r | %s | %s |" % (
            name, a, b, r.unified_type.name, r.precision.summary()))

    text = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()

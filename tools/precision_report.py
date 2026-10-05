"""精度报告：枚举所有可能丢精度的类型组合，实测并输出处理结果。"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from setops import Dataset, Type

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "precision_report.md")

CASES = [
    # (名称, 左类型, 左值, 右类型, 右值, 说明)
    ("int64(2^53+1) + float64", Type.INT64, [2 ** 53 + 1, 2 ** 53 - 1, 7],
     Type.FLOAT64, [0.5], "2^53+1 不可精确表示 -> 2^53；2^53-1 与 7 精确"),
    ("int64 边界 + float32", Type.INT64, [2 ** 63 - 1, -2 ** 63],
     Type.FLOAT32, [1.5], "int64 最大值 -> 9.223372036854776e18（丢失低位）"),
    ("bigint + float64", Type.BIGINT, [2 ** 70 + 12345, 2 ** 53],
     Type.FLOAT64, [0.25], "2^70+12345 丢失低位；2^53 精确"),
    ("bigint 超范围 + float64", Type.BIGINT, [10 ** 400, -(10 ** 400)],
     Type.FLOAT64, [1.0], "超出 float64 范围 -> ±inf，记录精度丢失"),
    ("decimal + float64", Type.DECIMAL,
     [Decimal("0.1"), Decimal("0.5"), Decimal("1e1000")],
     Type.FLOAT64, [2.5], "0.1 不可精确表示；0.5 精确；1e1000 -> inf"),
    ("decimal + float32", Type.DECIMAL, [Decimal("3.14"), Decimal("0.125")],
     Type.FLOAT32, [1.0], "3.14 不可精确表示；0.125 精确"),
    ("int32 + float64（对照：精确）", Type.INT32, [2 ** 31 - 1, -2 ** 31],
     Type.FLOAT64, [0.5], "int32 < 2^53，全部精确，无报告"),
]


def main():
    lines = [
        "# 精度报告",
        "",
        "策略：可能丢精度的提升（int64/bigint/decimal -> float）**继续运算**，",
        "每个丢失值计入 `PrecisionReport`（列、源/目标类型、丢失值数、示例行号与原值/转换值），",
        "绝不静默丢弃；精确组合不产生任何报告。",
        "",
        "| 用例 | 提升结果 | 丢失值数 | 报告明细 |",
        "|---|---|---|---|",
    ]
    detail_sections = []
    for name, lt, lvals, rt, rvals, note in CASES:
        a = Dataset([lt], [(v,) for v in lvals], name="left")
        b = Dataset([rt], [(v,) for v in rvals], name="right")
        r = a.union(b)
        loss = r.precision.total_loss_count
        lines.append(
            f"| {name} | {r.types[0].value} | {loss} | {note} |"
        )
        detail_sections.append(f"## {name}\n\n{r.precision.to_markdown()}\n")
    lines += ["", "## 各用例 PrecisionReport 明细", ""]
    doc = "\n".join(lines) + "\n".join(detail_sections)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"已生成 {OUT}")
    print(doc)


if __name__ == "__main__":
    main()

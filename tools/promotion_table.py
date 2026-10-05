"""由代码直接生成提升表文档，保证文档与实现一致。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from setops import Type, is_potentially_lossy, join

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "promotion_table.md")


def main():
    types = list(Type)
    lines = [
        "# 类型提升表（join 表）",
        "",
        "单元格 = 行类型与列类型提升后的结果类型；`✗` = 不可比，拒绝运算并报列位置；",
        "`⚠` 后缀 = 该组合可能丢精度，逐值检测并写入 PrecisionReport。",
        "",
        "提升规则摘要：",
        "",
        "1. `null + T -> T`（空值作为值参与运算，不改变结果类型）。",
        "2. 整数链 `bool < int32 < int64 < bigint`，取较高者。",
        "3. `decimal + 整数 -> decimal`（精确）；`decimal + float -> float64`（可能丢精度）。",
        "4. `bool + float32 -> float32`；`int32 + float32 -> float64`（避免 24 位尾数丢精度）；",
        "   `int64/bigint + float -> float64`（可能丢精度）；`float32 + float64 -> float64`。",
        "5. `date + datetime -> datetime`（date 取当日 00:00:00，精确）。",
        "6. string / bytes / 数值 / 日期 跨族组合一律 `✗`，绝不静默 str()。",
        "",
        "| 左 \\\\ 右 | " + " | ".join(t.value for t in types) + " |",
        "|" + "---|" * (len(types) + 1),
    ]
    for a in types:
        row = [f"**{a.value}**"]
        for b in types:
            t = join(a, b)
            if t is None:
                row.append("✗")
            else:
                mark = " ⚠" if is_potentially_lossy(a, b) else ""
                row.append(t.value + mark)
        lines.append("| " + " | ".join(row) + " |")
    lines += [
        "",
        "## 可能丢精度的组合（⚠）",
        "",
        "| 组合 | 提升结果 | 检测方式 | 处理 |",
        "|---|---|---|---|",
        "| int64 + float32/float64 | float64 | `int(float(v)) != v` 或溢出为 inf | 继续运算，计入 PrecisionReport |",
        "| bigint + float32/float64 | float64 | 同上（含超出 float 范围 -> ±inf） | 同上 |",
        "| decimal + float32/float64 | float64 | `Decimal.from_float(float(v)) != v` | 同上 |",
        "",
        "说明：`int32 + float32` 直接提升到 `float64`（而非 `float32`），从规则上消除该组合的精度丢失。",
        "",
    ]
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"已生成 {OUT}")


if __name__ == "__main__":
    main()

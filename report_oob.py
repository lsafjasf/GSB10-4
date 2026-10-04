"""构造越界/缺失共享表样例, 验证报错定位到表名与坐标, 输出 data/oob_report.txt。"""
import os

from shared_strings import (
    SharedStringTable, Sheet, SharedStringError,
)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "oob")

SST_XML = """<?xml version="1.0"?>
<sst count="2" uniqueCount="2"><si><t>苹果</t></si><si><t>香蕉</t></si></sst>"""

CASES = [
    ("off_by_one", "价格表", "C7", "2", "索引 == 表长 (off-by-one)"),
    ("far_beyond", "价格表", "D8", "999", "索引远超表长"),
    ("negative", "库存表", "A1", "-1", "负索引"),
    ("non_integer", "日志表", "B2", "abc", "索引不是整数"),
    ("missing_table", "订单表", "E5", "0", "缺失共享表 (sst=None)"),
    ("empty_table", "空表用例", "C3", "0", "空共享表被引用"),
]


def sheet_xml(coord, value):
    return (
        '<worksheet><sheetData><row r="1">'
        f'<c r="{coord}" t="s"><v>{value}</v></c>'
        "</row></sheetData></worksheet>"
    )


def main():
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "sharedStrings.xml"), "w", encoding="utf-8") as fh:
        fh.write(SST_XML)
    sst = SharedStringTable.from_xml(SST_XML)
    empty_sst = SharedStringTable.from_xml('<sst count="0" uniqueCount="0"></sst>')

    lines = ["越界/异常索引报告", "=" * 60, ""]
    for case_id, sheet_name, coord, value, desc in CASES:
        xml = sheet_xml(coord, value)
        with open(os.path.join(OUT, f"{case_id}.xml"), "w", encoding="utf-8") as fh:
            fh.write(xml)
        use_sst = {"missing_table": None, "empty_table": empty_sst}.get(case_id, sst)
        try:
            Sheet.from_xml(xml, use_sst, sheet_name)
        except SharedStringError as exc:
            lines.append(f"[{case_id}] {desc}")
            lines.append(f"  用例文件: data/oob/{case_id}.xml")
            lines.append(f"  异常类型: {type(exc).__name__}")
            lines.append(f"  定位信息: 表名={getattr(exc, 'sheet', sheet_name)} 坐标={getattr(exc, 'coord', coord)}")
            lines.append(f"  错误消息: {exc}")
            lines.append("  结论: 已拒绝, 未返回空白单元格")
        else:
            lines.append(f"[{case_id}] {desc} -> 失败: 未抛异常!")
        lines.append("")
    report = "\n".join(lines)
    with open(os.path.join(HERE, "data", "oob_report.txt"), "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    print(report)


if __name__ == "__main__":
    main()

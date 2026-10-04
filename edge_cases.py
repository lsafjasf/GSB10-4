"""边界用例独立运行器：逐项断言并把结果写入 out/edge_cases.txt。

运行: python3 edge_cases.py
"""
import os
import sys

from style_inherit import Document, Paragraph, Run, Style, expand
from style_inherit.cases import demo_document, make_random_document
from manual_reference import diff_expand

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
DEFAULTS = {"font": "D", "size": 10, "bold": False, "italic": False,
            "color": "gray", "underline": False}


def case_single_layer_defaults():
    doc = Document(defaults=dict(DEFAULTS), styles={},
                   paragraphs=[Paragraph([Run("x")])])
    got = expand(doc)
    row = list(got.iter_rows())[0]
    pairs = dict(zip(got.props, zip(row.values, row.sources)))
    assert all(src == "default" for _, src in pairs.values())
    return "6 个属性全部来自 default"


def case_single_layer_inline():
    doc = Document(defaults=dict(DEFAULTS), styles={},
                   paragraphs=[Paragraph([Run("x", {"bold": True})])])
    row = list(expand(doc).iter_rows())[0]
    assert row.values[row.sources.index("inline")] is True
    return "仅内联层定义 bold -> inline，其余 default"


def case_three_layer_conflict():
    styles = {"S": Style("S", {"bold": True, "size": 20, "color": "red"})}
    para = Paragraph([Run("x", {"bold": False})],
                     {"bold": True, "size": 14}, "S")
    doc = Document(defaults=dict(DEFAULTS), styles=styles, paragraphs=[para])
    expanded = expand(doc)
    row = list(expanded.iter_rows())[0]
    pairs = dict(zip(expanded.props, zip(row.values, row.sources)))
    assert pairs["bold"] == (False, "inline")
    assert pairs["size"] == (14, "paragraph")
    assert pairs["color"] == ("red", "style:S")
    assert pairs["font"] == ("D", "default")
    return "bold: inline>paragraph>style；size: paragraph>style；color: style"


def case_empty_style():
    doc = Document(defaults=dict(DEFAULTS),
                   styles={"E": Style("E", {})},
                   paragraphs=[Paragraph([Run("x")], {}, "E")])
    row = list(expand(doc).iter_rows())[0]
    assert set(row.sources) == {"default"}
    return "空样式不产生任何属性，6 个属性全部回退 default"


def case_empty_paragraph_and_run():
    doc = Document(defaults=dict(DEFAULTS),
                   paragraphs=[Paragraph([], {}, "E"),
                               Paragraph([Run(""), Run("")]),
                               Paragraph([Run("z", {})])])
    rows = list(expand(doc).iter_rows())
    assert len(rows) == 1 and rows[0].char == "z"
    return "空段落与空 run 不产生字符行，仅 'z' 一行"


def case_broken_style_ref():
    styles = {"O": Style("O", {"bold": True}, based_on="Ghost")}
    doc = Document(defaults=dict(DEFAULTS), styles=styles,
                   paragraphs=[Paragraph([Run("x")], {}, "O")])
    expanded = expand(doc)
    assert any(d.kind == "broken-style-ref" and d.style == "O"
               and d.missing == "Ghost" for d in expanded.diagnostics)
    row = list(expanded.iter_rows())[0]
    pairs = dict(zip(expanded.props, zip(row.values, row.sources)))
    assert pairs["bold"] == (True, "style:O")
    assert pairs["font"] == ("D", "default")
    return "报告 O->Ghost 断链；自有 bold 保留，其余显式回退 default"


def case_missing_paragraph_style():
    doc = Document(defaults=dict(DEFAULTS), styles={},
                   paragraphs=[Paragraph([Run("x")], {}, "Nope")])
    expanded = expand(doc)
    assert any(d.kind == "missing-paragraph-style" for d in expanded.diagnostics)
    assert set(list(expanded.iter_rows())[0].sources) == {"default"}
    return "段落引用缺失样式 -> 报告并按无样式处理"


def case_unresolved_property():
    doc = Document(defaults={"size": 10},
                   styles={"S": Style("S", {"kern": 2})},
                   paragraphs=[Paragraph([Run("a")], {}, "S"),
                               Paragraph([Run("b")])])
    expanded = expand(doc)
    rows = list(expanded.iter_rows())
    pairs = dict(zip(expanded.props, zip(rows[1].values, rows[1].sources)))
    assert pairs["kern"] == (None, "none")
    assert any(d.kind == "unresolved-property" and d.prop == "kern"
               for d in expanded.diagnostics)
    return "无任何层级提供 kern -> (null, none) 且逐段落报告，不静默丢弃"


def case_self_loop():
    styles = {"A": Style("A", {"bold": True}, based_on="A")}
    doc = Document(defaults=dict(DEFAULTS), styles=styles,
                   paragraphs=[Paragraph([Run("x")], {}, "A")])
    expanded = expand(doc)
    cycles = [d for d in expanded.diagnostics if d.kind == "cycle"]
    assert len(cycles) == 1 and cycles[0].cycle == ("A",)
    row = list(expanded.iter_rows())[0]
    pairs = dict(zip(expanded.props, zip(row.values, row.sources)))
    assert pairs["bold"] == (True, "style:A")
    return "自环 A->A 被识别，环上样式名 = A"


def case_two_node_cycle():
    styles = {
        "X": Style("X", {"bold": True, "size": 1}, based_on="Y"),
        "Y": Style("Y", {"italic": True, "size": 2}, based_on="X"),
    }
    doc = Document(defaults=dict(DEFAULTS), styles=styles,
                   paragraphs=[Paragraph([Run("x")], {}, "X"),
                               Paragraph([Run("y")], {}, "Y")])
    expanded = expand(doc)
    cycles = [d for d in expanded.diagnostics if d.kind == "cycle"]
    assert len(cycles) == 1 and set(cycles[0].cycle) == {"X", "Y"}
    rows = list(expanded.iter_rows())
    gx = dict(zip(expanded.props, zip(rows[0].values, rows[0].sources)))
    gy = dict(zip(expanded.props, zip(rows[1].values, rows[1].sources)))
    assert gx["size"] == (1, "style:X") and gy["size"] == (2, "style:Y")
    assert gx["italic"] == (True, "style:Y")
    return "环 X<->Y：报告环名；入口最近者胜出（X 段落 size=1，Y 段落 size=2）"


def case_cycle_with_prefix_and_broken_edge():
    styles = {
        "Root": Style("Root", {"font": "F"}, based_on="A"),
        "A": Style("A", {"size": 1}, based_on="B"),
        "B": Style("B", {"size": 2}, based_on="A"),
    }
    doc = Document(defaults=dict(DEFAULTS), styles=styles,
                   paragraphs=[Paragraph([Run("x")], {}, "Root")])
    expanded = expand(doc)
    cycles = [d for d in expanded.diagnostics if d.kind == "cycle"]
    assert set(cycles[0].cycle) == {"A", "B"}
    assert cycles[0].prefix == ("Root",)
    row = list(expanded.iter_rows())[0]
    pairs = dict(zip(expanded.props, zip(row.values, row.sources)))
    assert pairs["size"] == (1, "style:A") and pairs["font"] == ("F", "style:Root")
    return "带环外前驱 Root->环(A,B)：环报告含前驱，Root 覆盖环结果"


def case_deep_chain():
    depth = 10000
    styles = {"L0": Style("L0", {"size": 1}, based_on="L1")}
    for i in range(1, depth):
        based_on = "L%d" % (i + 1) if i + 1 < depth else None
        props = {"font": "deep"} if i + 1 == depth else {}
        styles["L%d" % i] = Style("L%d" % i, props, based_on=based_on)
    doc = Document(defaults=dict(DEFAULTS), styles=styles,
                   paragraphs=[Paragraph([Run("x")], {}, "L0")])
    expanded = expand(doc)
    row = list(expanded.iter_rows())[0]
    pairs = dict(zip(expanded.props, zip(row.values, row.sources)))
    assert pairs["font"] == ("deep", "style:L%d" % (depth - 1))
    return "1 万层继承链迭代解析成功，最深层 font 生效"


def case_unicode_rows():
    doc = Document(defaults=dict(DEFAULTS),
                   paragraphs=[Paragraph([Run("é中🙂\t")])])
    rows = list(expand(doc).iter_rows())
    assert [r.char for r in rows] == ["é", "中", "🙂", "\t"]
    return "多字节字符与 emoji 按 Unicode 码点逐字符成行（含制表符）"


def case_blank_based_on():
    styles = {"A": Style("A", {"bold": True}, based_on="   ")}
    doc = Document(defaults=dict(DEFAULTS), styles=styles,
                   paragraphs=[Paragraph([Run("x")], {}, "A")])
    expanded = expand(doc)
    assert expanded.diagnostics == []
    return "based_on 为空白串视为无父样式，不误报断链"


def case_demo_diff():
    problems = diff_expand(demo_document())
    assert problems == [], problems
    return "演示文档库展开与手工逐字符推算完全一致"


def case_random_diff_batch():
    for seed in range(100):
        problems = diff_expand(make_random_document(seed))
        assert not problems, "seed=%d: %s" % (seed, problems)
    return "100 个随机文档（含环/断链/空样式/未解析属性）逐字符对拍全一致"


CASES = [
    ("单层-仅默认值", case_single_layer_defaults),
    ("单层-仅内联", case_single_layer_inline),
    ("三层冲突-内联>段落>样式>默认", case_three_layer_conflict),
    ("空样式", case_empty_style),
    ("空段落与空run", case_empty_paragraph_and_run),
    ("断链-父样式不存在", case_broken_style_ref),
    ("段落引用缺失样式", case_missing_paragraph_style),
    ("未定义属性不静默丢失", case_unresolved_property),
    ("循环引用-自环", case_self_loop),
    ("循环引用-二元环", case_two_node_cycle),
    ("循环引用-带环外前驱", case_cycle_with_prefix_and_broken_edge),
    ("深链10000层", case_deep_chain),
    ("Unicode逐码点", case_unicode_rows),
    ("空白based_on归一化", case_blank_based_on),
    ("对拍-演示文档", case_demo_diff),
    ("对拍-100随机文档", case_random_diff_batch),
]


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    lines = []
    n_pass = 0
    for name, func in CASES:
        try:
            note = func()
            lines.append("PASS | %s | %s" % (name, note))
            n_pass += 1
        except AssertionError as exc:
            lines.append("FAIL | %s | %s" % (name, exc))
        except Exception as exc:  # 任何异常都算失败
            lines.append("ERROR | %s | %r" % (name, exc))
    lines.append("")
    lines.append("合计: %d/%d 通过" % (n_pass, len(CASES)))
    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT, "edge_cases.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    return 0 if n_pass == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())

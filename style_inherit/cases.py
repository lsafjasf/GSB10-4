"""测试/演示/基准用文档构造（确定性，可复现）。"""
import random
from typing import Optional

from .model import Document, Paragraph, Run, Style

PROP_NAMES = ("font", "size", "bold", "italic", "color", "underline")


def demo_document() -> Document:
    """覆盖全部要求场景的演示文档（10 个段落，23 个字符）。"""
    styles = {
        "Normal": Style("Normal", {"font": "宋体", "size": 12, "color": "black"}),
        "Heading": Style("Heading", {"size": 20, "bold": True}, based_on="Normal"),
        "Emph": Style("Emph", {"italic": True, "color": "crimson"},
                      based_on="Heading"),
        "Plain": Style("Plain", {}),                      # 空样式
        "Orphan": Style("Orphan", {"bold": True},
                        based_on="Ghost"),                # 断链：父样式不存在
        "CycA": Style("CycA", {"size": 18}, based_on="CycB"),
        "CycB": Style("CycB", {"color": "navy"}, based_on="CycC"),
        "CycC": Style("CycC", {"underline": True}, based_on="CycA"),
    }
    defaults = {"font": "默认体", "size": 10, "bold": False,
                "italic": False, "color": "gray", "underline": False}
    paragraphs = [
        # P0 三层冲突：inline/段落/样式/默认值 同时出现
        Paragraph([Run("标题", {"bold": False}), Run("正文", {})],
                  {"bold": True, "size": 14}, "Normal"),
        # P1 继承链两级 + 三层 color 冲突（inline > Emph > 默认）
        Paragraph([Run("强调文本", {"color": "gold"})], {}, "Emph"),
        # P2 单层：只有段落直连属性 + 默认值
        Paragraph([Run("无样式", {})], {"font": "黑体"}, None),
        # P3 断链：Orphan -> Ghost(不存在)，缺失部分回退默认值
        Paragraph([Run("断链", {"size": 16})], {}, "Orphan"),
        # P4 循环引用：CycA -> CycB -> CycC -> CycA
        Paragraph([Run("循环", {})], {}, "CycA"),
        # P5 段落引用不存在的样式
        Paragraph([Run("丢失", {})], {}, "Missing"),
        # P6 空样式：全部回退默认值
        Paragraph([Run("空样式", {})], {}, "Plain"),
        # P7 空段落（无 run）
        Paragraph([], {}, "Normal"),
        # P8 空 run（空文本）
        Paragraph([Run("", {})], {}, "Normal"),
        # P9 Unicode：多字节字符与 emoji 按码点计 1 字符
        Paragraph([Run("é中🙂", {"font": "Emoji"})], {}, None),
    ]
    return Document(defaults=defaults, styles=styles, paragraphs=paragraphs)


def make_random_document(seed: int, n_styles: int = 12, n_paras: int = 30,
                         allow_cycles: bool = True,
                         allow_missing: bool = True) -> Document:
    """确定性随机文档：随机样式链（含环/断链/空样式）、随机段落与内联。

    属性 "kern" 只出现在样式/段落/内联中、从不出现在默认值里，
    用于触发 unresolved-property 路径。
    """
    rng = random.Random(seed)
    names = ["S%d" % i for i in range(n_styles)]
    fonts = ["A", "B", "C"]
    colors = ["red", "green", "blue"]

    def random_props(allow_empty: bool):
        props = {}
        pool = list(PROP_NAMES) + ["kern"]
        for key in pool:
            if rng.random() < 0.35:
                if key in ("bold", "italic", "underline"):
                    props[key] = rng.random() < 0.5
                elif key == "size":
                    props[key] = rng.randint(8, 24)
                elif key == "font":
                    props[key] = rng.choice(fonts)
                elif key == "color":
                    props[key] = rng.choice(colors)
                else:
                    props[key] = rng.randint(0, 3)
        if not props and not allow_empty:
            props["size"] = rng.randint(8, 24)
        return props

    styles = {}
    for name in names:
        roll = rng.random()
        if allow_missing and roll < 0.10:
            based_on: Optional[str] = "GX%d" % rng.randint(0, 5)  # 断链
        elif roll < 0.75:
            based_on = rng.choice(names)  # 可能成环（含自环）
        else:
            based_on = None
        styles[name] = Style(name, random_props(allow_empty=True),
                             based_on=based_on)

    defaults = {"font": "D", "size": 10, "bold": False,
                "italic": False, "color": "black", "underline": False}
    if rng.random() < 0.3:
        del defaults[rng.choice(list(defaults))]  # 偶发缺省，触发 unresolved

    alphabet = "ab中🙂xy"
    paragraphs = []
    for _ in range(n_paras):
        roll = rng.random()
        if roll < 0.2:
            style = None
        elif allow_missing and roll < 0.3:
            style = "PX%d" % rng.randint(0, 5)  # 段落引用缺失样式
        else:
            style = rng.choice(names)
        runs = []
        for _ in range(rng.randint(0, 3)):
            text = "".join(rng.choice(alphabet)
                           for _ in range(rng.randint(0, 6)))
            runs.append(Run(text, random_props(allow_empty=True)))
        paragraphs.append(Paragraph(runs, random_props(allow_empty=True),
                                    style))
    return Document(defaults=defaults, styles=styles, paragraphs=paragraphs)


def bench_document(n_paras: int = 50000, n_styles: int = 200,
                   chain_depth: int = 40, seed: int = 20261005) -> Document:
    """基准文档：默认 5 万段落、10 万 run、100 万字符、40 层继承链，
    另含 1 条断链样式与 1 个二元环，贴近真实文档的混合负载。
    """
    rng = random.Random(seed)
    styles = {}
    for i in range(n_styles):
        based_on = "S%d" % (i - 1) if i % chain_depth else None
        props = {}
        for key in PROP_NAMES:
            if rng.random() < 0.3:
                if key in ("bold", "italic", "underline"):
                    props[key] = rng.random() < 0.5
                elif key == "size":
                    props[key] = rng.randint(9, 32)
                elif key == "font":
                    props[key] = "F%d" % rng.randint(0, 9)
                else:
                    props[key] = "c%d" % rng.randint(0, 9)
        styles["S%d" % i] = Style("S%d" % i, props, based_on=based_on)
    styles["Broken1"] = Style("Broken1", {"bold": True}, based_on="NoSuch")
    styles["CycP"] = Style("CycP", {"size": 15}, based_on="CycQ")
    styles["CycQ"] = Style("CycQ", {"color": "purple"}, based_on="CycP")

    defaults = {"font": "F0", "size": 11, "bold": False,
                "italic": False, "color": "c0", "underline": False}
    words = "the quick brown fox jumps over lazy dog 文档展开性能测试 "
    paragraphs = []
    for i in range(n_paras):
        if i % 1237 == 0:
            style: Optional[str] = "CycP"
        elif i % 997 == 0:
            style = "Broken1"
        elif i % 991 == 0:
            style = "Lost"  # 段落引用缺失样式
        else:
            style = "S%d" % (i % n_styles)
        para_props = {"size": 9 + i % 20} if i % 3 == 0 else {}
        text_a = (words + words)[:20]
        text_b = (words[::-1] + words)[:20]
        inline = {"bold": bool(i % 2)} if i % 5 == 0 else {}
        paragraphs.append(Paragraph(
            [Run(text_a, inline), Run(text_b, {})], para_props, style))
    return Document(defaults=defaults, styles=styles, paragraphs=paragraphs)

"""style_expand 自测：单层 / 三层冲突 / 空样式 / 循环引用 / 断链 / 超深链 / 对拍。"""

import unittest

from style_expand import DEFAULT_PROPS, StyleSheet, expand_document
from manual_expand import manual_expand


def make_doc():
    return [
        {  # 0: 只有样式层
            "style": "Body", "props": {}, "text": "ab", "spans": [],
        },
        {  # 1: 三层冲突：size 三层都写，bold 两层，color 仅样式
            "style": "Body",
            "props": {"size": 14, "bold": True},
            "text": "xy",
            "spans": [(0, 1, {"size": 20})],
        },
        {  # 2: 空样式（无属性、无父类）
            "style": "Empty", "props": {}, "text": "z", "spans": [],
        },
        {  # 3: 引用不存在的样式
            "style": "Ghost", "props": {"italic": True}, "text": "g", "spans": [],
        },
        {  # 4: 循环引用样式
            "style": "LoopA", "props": {}, "text": "L", "spans": [],
        },
    ]


def make_styles():
    return {
        "Root":  {"props": {"font": "sans", "size": 11, "color": "gray"}, "based_on": None},
        "Body":  {"props": {"size": 12, "color": "blue"}, "based_on": "Root"},
        "Empty": {"props": {}, "based_on": None},
        "LoopA": {"props": {"bold": True}, "based_on": "LoopB"},
        "LoopB": {"props": {"size": 99}, "based_on": "LoopA"},
        "Orphan": {"props": {"color": "red"}, "based_on": "NoSuch"},
    }


class TestLayers(unittest.TestCase):
    def setUp(self):
        self.sheet = StyleSheet(make_styles())
        self.doc = make_doc()
        self.table = expand_document(self.sheet, self.doc)

    def test_single_layer(self):
        # 段落 0：全部来自样式链 Body<-Root
        a, b = self.table[0]
        self.assertEqual(a["font"], ("sans", "style:Body"))
        self.assertEqual(a["size"], (12, "style:Body"))
        self.assertEqual(a["color"], ("blue", "style:Body"))
        self.assertEqual(a["bold"], (False, "default"))
        self.assertEqual(b, a)

    def test_three_layer_conflict(self):
        x, y = self.table[1]
        # 字符 x：内联 size=20 胜出；bold 段落层胜出；color 只有样式层
        self.assertEqual(x["size"], (20, "inline"))
        self.assertEqual(x["bold"], (True, "paragraph"))
        self.assertEqual(x["color"], ("blue", "style:Body"))
        # 字符 y：无内联，size 落到段落层
        self.assertEqual(y["size"], (14, "paragraph"))
        self.assertEqual(y["bold"], (True, "paragraph"))

    def test_empty_style(self):
        (z,) = self.table[2]
        for prop in DEFAULT_PROPS:
            self.assertEqual(z[prop], (DEFAULT_PROPS[prop], "default"))

    def test_broken_reference(self):
        (g,) = self.table[3]
        self.assertEqual(g["italic"], (True, "paragraph"))       # 段落层不丢
        self.assertEqual(g["font"], ("serif", "default"))        # 样式缺失 -> 默认值
        self.assertTrue(any("Ghost" in d and "断裂" in d for d in self.sheet.diagnostics))

    def test_cycle_reported(self):
        (L,) = self.table[4]
        cyc = [d for d in self.sheet.diagnostics if "循环" in d]
        self.assertEqual(len(cyc), 1)
        self.assertIn("LoopA", cyc[0])
        self.assertIn("LoopB", cyc[0])
        # 环上样式自身显式属性仍生效，未设置的回退默认
        self.assertEqual(L["bold"], (True, "style:LoopA"))
        self.assertEqual(L["font"], ("serif", "default"))

    def test_orphan_chain(self):
        sheet = StyleSheet(make_styles())
        doc = [{"style": "Orphan", "props": {}, "text": "o", "spans": []}]
        (o,) = expand_document(sheet, doc)[0]
        self.assertEqual(o["color"], ("red", "style:Orphan"))    # 断点之前的属性保留
        self.assertEqual(o["size"], (12, "default"))             # 断点之后回退默认
        self.assertTrue(any("NoSuch" in d for d in sheet.diagnostics))

    def test_deep_chain_no_recursion_limit(self):
        depth = 20000  # 远超 Python 递归上限，验证迭代实现
        styles = {"S0": {"props": {"font": "mono"}, "based_on": None}}
        for i in range(1, depth):
            styles[f"S{i}"] = {"props": {}, "based_on": f"S{i-1}"}
        styles[f"S{depth-1}"]["props"] = {"size": 7}
        sheet = StyleSheet(styles)
        doc = [{"style": f"S{depth-1}", "props": {}, "text": "d", "spans": []}]
        (d,) = expand_document(sheet, doc)[0]
        self.assertEqual(d["font"], ("mono", f"style:S{depth-1}"))
        self.assertEqual(d["size"], (7, f"style:S{depth-1}"))

    def test_cross_check_against_manual(self):
        # 对拍：快速实现 vs 逐层手工推算，逐字符一致
        manual, _ = manual_expand(make_styles(), self.doc)
        self.assertEqual(self.table, manual)


if __name__ == "__main__":
    unittest.main(verbosity=2)

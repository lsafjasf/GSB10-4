"""单元测试：覆盖单层、三层冲突、空样式、循环引用、断链、深链等情形，
并对 demo 文档与 200 个随机种子文档做库 vs 手工推算的全量对拍。

运行: python3 tests.py  （或 python3 -m unittest tests -v）
"""
import unittest

from style_inherit import Document, Paragraph, Run, Style, expand
from style_inherit.cases import demo_document, make_random_document
from manual_reference import diff_expand

DEFAULTS = {"font": "D", "size": 10, "bold": False, "italic": False,
            "color": "gray", "underline": False}


def one_char_doc(**kwargs):
    """构造只有 1 个字符 'x' 的文档，返回 (doc, expanded, (值,来源) 字典)。"""
    doc = Document(defaults=dict(DEFAULTS), **kwargs)
    expanded = expand(doc)
    rows = list(expanded.iter_rows())
    assert len(rows) == 1
    row = rows[0]
    pairs = dict(zip(expanded.props, zip(row.values, row.sources)))
    return doc, expanded, pairs


class TestLayers(unittest.TestCase):
    def test_single_layer_defaults_only(self):
        _, _, got = one_char_doc(
            styles={}, paragraphs=[Paragraph([Run("x")])])
        self.assertTrue(all(src == "default" for _, src in got.values()))
        self.assertEqual(got["font"], ("D", "default"))

    def test_single_layer_inline_only(self):
        _, _, got = one_char_doc(
            styles={},
            paragraphs=[Paragraph([Run("x", {"bold": True})])])
        self.assertEqual(got["bold"], (True, "inline"))
        self.assertEqual(got["size"], (10, "default"))

    def test_three_layer_conflict(self):
        styles = {"S": Style("S", {"bold": True, "size": 20, "color": "red"})}
        paragraphs = [Paragraph([Run("x", {"bold": False})],
                                {"bold": True, "size": 14}, "S")]
        _, _, got = one_char_doc(styles=styles, paragraphs=paragraphs)
        self.assertEqual(got["bold"], (False, "inline"))
        self.assertEqual(got["size"], (14, "paragraph"))
        self.assertEqual(got["color"], ("red", "style:S"))
        self.assertEqual(got["font"], ("D", "default"))

    def test_style_chain_inheritance(self):
        styles = {
            "A": Style("A", {"font": "F", "size": 12}),
            "B": Style("B", {"size": 20}, based_on="A"),
            "C": Style("C", {"color": "red"}, based_on="B"),
        }
        _, _, got = one_char_doc(
            styles=styles, paragraphs=[Paragraph([Run("x")], {}, "C")])
        self.assertEqual(got["color"], ("red", "style:C"))
        self.assertEqual(got["size"], (20, "style:B"))
        self.assertEqual(got["font"], ("F", "style:A"))


class TestEmptyAndBroken(unittest.TestCase):
    def test_empty_style_falls_back_to_defaults(self):
        styles = {"E": Style("E", {})}
        _, _, got = one_char_doc(
            styles=styles, paragraphs=[Paragraph([Run("x")], {}, "E")])
        self.assertTrue(all(src == "default" for _, src in got.values()))

    def test_empty_paragraph_and_empty_run(self):
        doc = Document(defaults=dict(DEFAULTS), styles={},
                       paragraphs=[Paragraph([]), Paragraph([Run("")])])
        expanded = expand(doc)
        self.assertEqual(list(expanded.iter_rows()), [])

    def test_broken_style_ref_reports_and_falls_back(self):
        styles = {"O": Style("O", {"bold": True}, based_on="Ghost")}
        _, expanded, got = one_char_doc(
            styles=styles, paragraphs=[Paragraph([Run("x")], {}, "O")])
        kinds = [d.kind for d in expanded.diagnostics]
        self.assertIn("broken-style-ref", kinds)
        diag = next(d for d in expanded.diagnostics
                    if d.kind == "broken-style-ref")
        self.assertEqual((diag.style, diag.missing), ("O", "Ghost"))
        self.assertEqual(got["bold"], (True, "style:O"))
        self.assertEqual(got["font"], ("D", "default"))

    def test_missing_paragraph_style(self):
        _, expanded, got = one_char_doc(
            styles={}, paragraphs=[Paragraph([Run("x")], {}, "Nope")])
        kinds = [d.kind for d in expanded.diagnostics]
        self.assertIn("missing-paragraph-style", kinds)
        self.assertTrue(all(src == "default" for _, src in got.values()))

    def test_unresolved_property_is_explicit_null(self):
        styles = {"S": Style("S", {"kern": 2})}
        _, _, got = one_char_doc(
            styles=styles, paragraphs=[Paragraph([Run("x")], {}, "S")])
        self.assertEqual(got["kern"], (2, "style:S"))
        # kern 只在 P0 的样式里出现、不在默认值里：
        # P1 的字符拿不到 kern，必须显式置 null 并报 unresolved-property
        doc = Document(defaults={"size": 10},
                       styles={"S": Style("S", {"kern": 2})},
                       paragraphs=[Paragraph([Run("a")], {}, "S"),
                                   Paragraph([Run("b")])])
        expanded = expand(doc)
        rows = list(expanded.iter_rows())
        got_b = dict(zip(expanded.props, zip(rows[1].values, rows[1].sources)))
        self.assertEqual(got_b["kern"], (None, "none"))
        kinds = [d.kind for d in expanded.diagnostics]
        self.assertIn("unresolved-property", kinds)


class TestCycles(unittest.TestCase):
    def test_self_loop(self):
        styles = {"A": Style("A", {"bold": True}, based_on="A")}
        _, expanded, got = one_char_doc(
            styles=styles, paragraphs=[Paragraph([Run("x")], {}, "A")])
        cycles = [d for d in expanded.diagnostics if d.kind == "cycle"]
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].cycle, ("A",))
        self.assertEqual(got["bold"], (True, "style:A"))

    def test_two_node_cycle_reports_ring_names(self):
        styles = {
            "X": Style("X", {"bold": True, "size": 1}, based_on="Y"),
            "Y": Style("Y", {"italic": True, "size": 2}, based_on="X"),
        }
        doc = Document(defaults=dict(DEFAULTS), styles=styles,
                       paragraphs=[Paragraph([Run("x")], {}, "X"),
                                   Paragraph([Run("y")], {}, "Y")])
        expanded = expand(doc)
        cycles = [d for d in expanded.diagnostics if d.kind == "cycle"]
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0].cycle), {"X", "Y"})
        rows = list(expanded.iter_rows())
        got_x = dict(zip(expanded.props, zip(rows[0].values, rows[0].sources)))
        got_y = dict(zip(expanded.props, zip(rows[1].values, rows[1].sources)))
        self.assertEqual(got_x["size"], (1, "style:X"))
        self.assertEqual(got_y["size"], (2, "style:Y"))
        self.assertEqual(got_x["italic"], (True, "style:Y"))
        self.assertEqual(got_y["bold"], (True, "style:X"))

    def test_deep_chain_no_recursion_limit(self):
        depth = 5000
        styles = {"L0": Style("L0", {"size": 1}, based_on="L1")}
        for i in range(1, depth):
            based_on = "L%d" % (i + 1) if i + 1 < depth else None
            props = {"font": "deep"} if i + 1 == depth else {}
            styles["L%d" % i] = Style("L%d" % i, props, based_on=based_on)
        _, _, got = one_char_doc(
            styles=styles, paragraphs=[Paragraph([Run("x")], {}, "L0")])
        self.assertEqual(got["font"], ("deep", "style:L%d" % (depth - 1)))
        self.assertEqual(got["size"], (1, "style:L0"))


class TestUnicode(unittest.TestCase):
    def test_codepoint_per_row(self):
        doc = Document(defaults=dict(DEFAULTS), styles={},
                       paragraphs=[Paragraph([Run("é中🙂")])])
        expanded = expand(doc)
        rows = list(expanded.iter_rows())
        self.assertEqual([r.char for r in rows], ["é", "中", "🙂"])
        self.assertEqual([r.char_index for r in rows], [0, 1, 2])


class TestDiff(unittest.TestCase):
    def test_demo_document_matches_manual(self):
        problems = diff_expand(demo_document())
        self.assertEqual(problems, [])

    def test_random_documents_match_manual(self):
        for seed in range(200):
            problems = diff_expand(make_random_document(seed))
            self.assertEqual(problems, [], "seed=%d: %s" % (seed, problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""template_engine 自测（标准库 unittest）。

运行: python3 -m unittest -v  或  python3 test_template_engine.py
"""

import unittest

from template_engine import (
    CycleError,
    MissingVariableError,
    TemplateSyntaxError,
    format_diff,
    render,
    structural_diff,
)


def assert_structure_ok(testcase, template, variables):
    rendered = render(template, variables)
    segments = structural_diff(template, rendered, variables)
    for seg in segments:
        testcase.assertTrue(seg.ok, f"结构对拍失败: {seg}\n{format_diff(segments)}")
    return rendered


class TestNoPlaceholder(unittest.TestCase):
    def test_plain_text_unchanged(self):
        tpl = "没有任何占位符的纯文本, hello {single} braces } ok"
        self.assertEqual(render(tpl, {"x": 1}), tpl)

    def test_empty_template(self):
        self.assertEqual(render("", {}), "")

    def test_structure_diff_on_plain_text(self):
        tpl = "纯文本 {a} } {"
        assert_structure_ok(self, tpl, {})


class TestBasicSubstitution(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(render("你好, {{name}}!", {"name": "世界"}), "你好, 世界!")

    def test_whitespace_and_multiple(self):
        tpl = "{{ a }}+{{b}}={{  c   }}"
        self.assertEqual(render(tpl, {"a": 1, "b": 2, "c": 3}), "1+2=3")

    def test_non_string_values(self):
        self.assertEqual(render("n={{n}}", {"n": 42}), "n=42")

    def test_user_braces_do_not_break_structure(self):
        # 用户内容里出现的花括号不得撑坏模板结构
        tpl = "JSON: {{payload}} END"
        variables = {"payload": '{"a": {1, 2}, "b": [3]}'}
        rendered = assert_structure_ok(self, tpl, variables)
        self.assertEqual(rendered, 'JSON: {"a": {1, 2}, "b": [3]} END')

    def test_user_unclosed_double_brace_is_literal(self):
        # 用户内容里未闭合的 {{ 退化为字面量, 不报错也不撑坏结构
        tpl = "[{{v}}]"
        rendered = assert_structure_ok(self, tpl, {"v": "100% {{sure"})
        self.assertEqual(rendered, "[100% {{sure]")


class TestEscapes(unittest.TestCase):
    def test_escaped_opening_brace(self):
        self.assertEqual(render(r"\{{name}}", {"name": "X"}), "{{name}}")

    def test_escaped_backslash_then_placeholder(self):
        # \\ 还原成 \, 后面的 {{name}} 仍是占位符
        self.assertEqual(render(r"\\{{name}}", {"name": "X"}), r"\X")

    def test_escaped_closing_brace(self):
        self.assertEqual(render(r"a\}}b", {}), "a}}b")

    def test_lone_backslash_kept(self):
        self.assertEqual(render(r"C:\path\x={{x}}", {"x": 1}), r"C:\path\x=1")

    def test_escape_restored_in_output(self):
        # 转义后的花括号在输出里必须还原成字面量
        tpl = r"字面量: \{{ not_a_var \}} 变量: {{v}}"
        self.assertEqual(render(tpl, {"v": "V"}), "字面量: {{ not_a_var }} 变量: V")

    def test_structure_diff_with_escapes(self):
        tpl = r"\{{keep}} and {{real}} \\end"
        rendered = assert_structure_ok(self, tpl, {"real": "R"})
        self.assertEqual(rendered, "{{keep}} and R \\end".replace("\\\\", "\\"))


class TestNesting(unittest.TestCase):
    def test_one_level_nesting(self):
        variables = {"outer": "A{{inner}}Z", "inner": "*"}
        self.assertEqual(render("{{outer}}", variables), "A*Z")

    def test_multi_level_nesting(self):
        variables = {
            "a": "1{{b}}2",
            "b": "3{{c}}4",
            "c": "5{{d}}6",
            "d": "!",
        }
        rendered = assert_structure_ok(self, "x={{a}}", variables)
        self.assertEqual(rendered, "x=135!642")

    def test_nested_in_template_and_value(self):
        tpl = "greeting={{greet}}, target={{who}}"
        variables = {"greet": "Hello {{who}}", "who": "Bob"}
        self.assertEqual(render(tpl, variables), "greeting=Hello Bob, target=Bob")

    def test_cycle_detected(self):
        variables = {"a": "{{b}}", "b": "{{a}}"}
        with self.assertRaises(CycleError) as ctx:
            render("{{a}}", variables)
        self.assertIn("a -> b -> a", str(ctx.exception))

    def test_self_cycle_detected(self):
        with self.assertRaises(CycleError):
            render("{{loop}}", {"loop": "{{loop}}"})

    def test_indirect_cycle_chain_reported(self):
        variables = {"a": "{{b}}", "b": "{{c}}", "c": "{{a}}"}
        with self.assertRaises(CycleError) as ctx:
            render("{{a}}", variables)
        self.assertEqual(ctx.exception.chain, ["a", "b", "c", "a"])


class TestMissingVariable(unittest.TestCase):
    def test_missing_reports_available(self):
        with self.assertRaises(MissingVariableError) as ctx:
            render("{{known}} {{ghost}}", {"known": 1, "other": 2})
        msg = str(ctx.exception)
        self.assertIn("'ghost'", msg)
        self.assertIn("known", msg)
        self.assertIn("other", msg)

    def test_missing_lists_empty_available(self):
        with self.assertRaises(MissingVariableError) as ctx:
            render("{{x}}", {})
        self.assertIn("无任何已定义变量", str(ctx.exception))

    def test_missing_in_nested_value(self):
        variables = {"a": "{{b}}", "c": "ok"}
        with self.assertRaises(MissingVariableError) as ctx:
            render("{{a}}", variables)
        self.assertIn("'b'", str(ctx.exception))

    def test_nothing_silently_dropped(self):
        # 缺失变量时不得产出半成品: 异常抛出, 无返回值
        try:
            render("keep {{gone}} keep", {"x": 1})
        except MissingVariableError:
            pass
        else:
            self.fail("应当抛出 MissingVariableError")


class TestTemplateSyntaxError(unittest.TestCase):
    def test_unclosed_placeholder_in_template(self):
        with self.assertRaises(TemplateSyntaxError):
            render("abc {{oops", {})

    def test_invalid_name(self):
        with self.assertRaises(TemplateSyntaxError):
            render("{{1bad}}", {})


class TestStructuralDiff(unittest.TestCase):
    def test_diff_all_ok(self):
        tpl = r"A {{x}} B \{{lit}} C {{y}} D"
        variables = {"x": "1", "y": "2"}
        segments = structural_diff(tpl, render(tpl, variables), variables)
        self.assertTrue(all(s.ok for s in segments))
        kinds = [s.kind for s in segments]
        self.assertEqual(kinds.count("placeholder"), 2)

    def test_diff_detects_tampering(self):
        tpl = "A {{x}} B"
        variables = {"x": "1"}
        tampered = "A 1 B!"   # 尾部被改动
        segments = structural_diff(tpl, tampered, variables)
        self.assertFalse(all(s.ok for s in segments))
        self.assertEqual(segments[-1].kind, "trailing")

    def test_diff_detects_literal_change(self):
        tpl = "left {{x}} right"
        variables = {"x": "1"}
        tampered = "left 1 RIGHT"
        segments = structural_diff(tpl, tampered, variables)
        self.assertFalse(all(s.ok for s in segments))

    def test_format_diff_output(self):
        tpl = "v={{v}}"
        variables = {"v": "9"}
        text = format_diff(structural_diff(tpl, render(tpl, variables), variables))
        self.assertIn("全部一致", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

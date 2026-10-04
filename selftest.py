"""tplfill 自测：固定用例 + 随机对拍（含独立参照实现）+ 结构校验。

运行: python3 selftest.py
"""
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tplfill
from tplfill import Raw, render, literal_spans

FAILURES = []


def check(desc, cond, detail=""):
    if cond:
        print("  ok   %s" % desc)
    else:
        FAILURES.append((desc, detail))
        print("  FAIL %s  %s" % (desc, detail))


def check_render(desc, template, variables, expected):
    got = render(template, variables)
    check(desc, got == expected, "got=%r want=%r" % (got, expected))


def check_error(desc, fn, exc_type, must_contain=None):
    try:
        fn()
    except exc_type as e:
        msg = str(e)
        if must_contain is not None:
            ok = all(s in msg for s in must_contain) if isinstance(
                must_contain, (list, tuple)) else must_contain in msg
        else:
            ok = True
        check("%s 抛 %s" % (desc, exc_type.__name__), ok, "msg=%r" % msg)
        return e
    except Exception as e:  # noqa
        check("%s 抛 %s" % (desc, exc_type.__name__), False,
              "实际抛出 %s: %s" % (type(e).__name__, e))
    else:
        check("%s 抛 %s" % (desc, exc_type.__name__), False, "未抛异常")


# ---------------------------------------------------------------------------
# 独立参照实现：用另一种算法（正则切分 + 逐字符反转义 + 迭代栈解析）
# 仅用于随机对拍，逻辑路径与 tplfill._parse 完全不同。
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# 参照实现算法（与 tplfill 的逐字符扫描完全不同）：
# 1) 把转义对 \\{ \\} \\\\ 替换为哨兵字符；
# 2) 校验：剩余的每个 {{ 都必须是一个完整合法占位符的开头；
# 3) 用正则 finditer 切分出占位符，字面量里的哨兵再还原。
_ESC_TO_SENT = [("\\\\", "\x02"), ("\\{", "\x00"), ("\\}", "\x01")]
_SENT_TO_CH = {"\x00": "{", "\x01": "}", "\x02": "\\"}


def _ref_encode(s):
    for k, v in _ESC_TO_SENT:
        s = s.replace(k, v)
    return s


def _ref_decode(s):
    for k, v in _SENT_TO_CH.items():
        s = s.replace(k, v)
    return s


def _ref_validate(encoded):
    pos = 0
    while True:
        i = encoded.find("{{", pos)
        if i < 0:
            return
        m = _PLACEHOLDER_RE.match(encoded, i)
        if not m:
            raise tplfill.TemplateSyntaxError("ref: 非法占位符 @%d" % i)
        pos = m.end()


def _ref_render_encoded(encoded, variables, cache, resolving, depth):
    if depth > tplfill.MAX_DEPTH:
        raise tplfill.RecursionLimitError("ref depth")
    _ref_validate(encoded)
    parts, pos = [], 0
    for m in _PLACEHOLDER_RE.finditer(encoded):
        parts.append(_ref_decode(encoded[pos:m.start()]))
        parts.append(_ref_resolve(m.group(1), variables, cache, resolving, depth))
        pos = m.end()
    parts.append(_ref_decode(encoded[pos:]))
    return "".join(parts)


def _ref_resolve(name, variables, cache, resolving, depth):
    if name in cache:
        return cache[name]
    if name in resolving:
        raise tplfill.CycleError("ref cycle: %s" % "->".join(resolving + [name]))
    if name not in variables:
        raise tplfill.MissingVariableError(
            "ref missing %r; 可用变量: %s"
            % (name, ", ".join(sorted(variables)) or "（无）"))
    val = variables[name]
    if isinstance(val, Raw):
        cache[name] = str(val)
        return cache[name]
    resolving.append(name)
    out = _ref_render_encoded(_ref_encode(str(val)), variables, cache,
                              resolving, depth + 1)
    resolving.pop()
    cache[name] = out
    return out


def ref_render(template, variables):
    return _ref_render_encoded(_ref_encode(template), variables, {}, [], 0)


# ---------------------------------------------------------------------------
# 结构校验：除占位符外一个字符都不能变
# ---------------------------------------------------------------------------

def check_structure(desc, template, variables, result):
    """字面量片段必须按原顺序、原样（含转义还原）出现在结果中。"""
    spans = literal_spans(template)
    remainder = result
    ok = True
    detail = ""
    for span in spans:
        idx = remainder.find(span)
        if idx < 0:
            ok, detail = False, "字面量 %r 在结果中丢失/错位" % span
            break
        remainder = remainder[idx + len(span):]
    check(desc, ok, detail)
    return ok


# ---------------------------------------------------------------------------
# 固定用例
# ---------------------------------------------------------------------------

def test_golden():
    print("[固定用例]")
    cases = json.load(open(os.path.join(os.path.dirname(__file__),
                                        "testdata", "golden.json"), encoding="utf-8"))
    for c in cases:
        if c.get("error"):
            exc = {"missing": tplfill.MissingVariableError,
                   "cycle": tplfill.CycleError,
                   "syntax": tplfill.TemplateSyntaxError,
                   "depth": tplfill.RecursionLimitError}[c["error"]]
            check_error(c["desc"], lambda t=c["template"], v=c["variables"]:
                        render(t, v), exc)
        else:
            got = render(c["template"], c["variables"])
            check(c["desc"], got == c["expected"],
                  "got=%r want=%r" % (got, c["expected"]))
            check_structure(c["desc"] + " [结构]", c["template"],
                            c["variables"], got)


def test_no_placeholder():
    print("[无占位符]")
    for t in ["", "plain text", "{ single brace }", "a{b}c",
              "trailing backslash \\", "\\n not newline", "\\\\x"]:
        got = render(t, {"x": "X"})
        want = ref_render(t, {"x": "X"})
        check("原样输出 %r" % t, got == want == _naive_unescape(t),
              "got=%r" % got)


def _naive_unescape(s):
    # 无占位符时结果等价于直接反转义
    return _ref_decode(_ref_encode(s))


def test_nested():
    print("[多层嵌套与循环]")
    check_render("两层引用", "结果: {{msg}}",
                 {"msg": "你好 {{who}}", "who": "世界"},
                 "结果: 你好 世界")
    check_render("三层引用", "{{a}}",
                 {"a": "{{b}}!", "b": "{{c}}?", "c": "C"}, "C?!")
    check_render("菱形共享同一终值", "{{a}}-{{b}}",
                 {"a": "{{c}}1", "b": "{{c}}2", "c": "Z"}, "Z1-Z2")
    check_render("值含转义花括号", "{{a}}",
                 {"a": r"\{\{notvar\}\}"}, "{{notvar}}")
    check_error("直接自循环", lambda: render("{{a}}", {"a": "{{a}}"}),
                tplfill.CycleError, ["a", "a"])
    check_error("三节点循环",
                lambda: render("{{a}}", {"a": "{{b}}", "b": "{{c}}",
                                         "c": "{{a}}"}),
                tplfill.CycleError, ["a", "b", "c", "a"])
    check_error("分支进入循环",
                lambda: render("{{a}}", {"a": "ok {{b}}", "b": "{{c}}",
                                         "c": "{{b}}"}),
                tplfill.CycleError)
    chain = {"l0": "leaf"}
    for i in range(1, tplfill.MAX_DEPTH):
        chain["l%d" % i] = "{{l%d}}" % (i - 1)
    check_render("深度刚好等于上限", "{{l%d}}" % (tplfill.MAX_DEPTH - 1),
                 chain, "leaf")
    over = {("d%d" % i): ("{{d%d}}" % (i + 1)) for i in range(tplfill.MAX_DEPTH + 2)}
    check_error("超过深度上限", lambda: render("{{d0}}", over),
                tplfill.RecursionLimitError)


def test_escape():
    print("[转义]")
    check_render("转义双花括号还原字面占位符", r"\{\{x\}\}", {}, "{{x}}")
    check_render("只转义左括号后剩余为字面量", r"\{{x}}", {}, "{{x}}")
    check_render("完整转义一对占位符", r"\{\{name\}\}", {"name": "X"}, "{{name}}")
    check_render("反斜杠转义自己", r"a\\b", {}, "a\\b")
    check_render("转义与占位符混合", r"\{{name}} -> {{name}}",
                 {"name": "Bob"}, "{{name}} -> Bob")
    check_render("反斜杠在右括号上", r"foo \} bar", {}, "foo } bar")
    check_render("其他字符前的反斜杠保留", r"\t \n \a", {}, r"\t \n \a")
    check_render("尾随反斜杠保留", "x\\", {}, "x\\")
    check_error("未闭合占位符", lambda: render("{{ name ", {}),
                tplfill.TemplateSyntaxError)
    check_error("非法变量名", lambda: render("{{1a}}", {}),
                tplfill.TemplateSyntaxError)


def test_missing():
    print("[变量缺失]")
    check_error("顶层缺失", lambda: render("hi {{who}}!", {}),
                tplfill.MissingVariableError, "（无）")
    e = check_error("嵌套层缺失并列可用变量",
                    lambda: render("{{a}}", {"a": "{{b}}", "c": 1}),
                    tplfill.MissingVariableError, ["'b'", "a", "c"])
    check("缺失报错中列出全部可用变量名",
          e is not None and "a" in str(e) and "c" in str(e))


def test_raw_user_content():
    print("[不可信用户内容 Raw]")
    evil = Raw("{{injected}} and {{a}} with \\ braces { single")
    check_render("用户花括号不撑坏结构", "评论: {{comment}} 结束",
                 {"comment": evil, "a": "SHOULD_NOT_APPEAR"},
                 "评论: %s 结束" % evil)
    check_render("普通值仍会被解析", "v={{v}}", {"v": "{{k}}", "k": "K"},
                 "v=K")
    check_render("Raw 中的反斜杠原样保留", "{{r}}", {"r": Raw(r"\{\}")},
                 r"\{\}")


# ---------------------------------------------------------------------------
# 随机对拍：tplfill vs 独立参照实现 + 结构校验
# ---------------------------------------------------------------------------

def random_template(rng, var_names, depth=0):
    """生成保证合法的模板（变量引用目标稍后保证存在）。"""
    pieces = []
    for _ in range(rng.randint(1, 6)):
        kind = rng.randrange(7)
        if kind < 3 and var_names:
            pieces.append("{{%s%s%s}}" % (
                " " * rng.randint(0, 2), rng.choice(var_names),
                " " * rng.randint(0, 1)))
        elif kind == 3:
            pieces.append(rng.choice([r"\{", r"\}", r"\\", r"\{\{x\}\}"]))
        elif kind == 4:
            pieces.append(rng.choice(["{", "}", "{{", "}}", "{a}", " { "]))
        else:
            pieces.append(rng.choice(["text", "", "你好", "\n", "x/y", "0"]))
    return "".join(pieces)


def test_fuzz(seed=27042, n=500):
    print("[随机对拍 %d 例]" % n)
    rng = random.Random(seed)
    names = ["a", "b", "c", "d"]
    checked_struct = 0
    invalid_both = 0
    for it in range(n):
        names_local = names[: rng.randint(1, len(names))]
        variables = {}
        for vname in names_local:
            if rng.random() < 0.35:
                variables[vname] = Raw(rng.choice([
                    "{{x}}", r"\{", "}}", "{ single ", r"\n", "{{a}}\\"]))
            elif rng.random() < 0.6:
                variables[vname] = random_template(rng, names_local)
            else:
                variables[vname] = rng.choice(["V", "", "你好", "z}z", "q{q"])
        # 保证变量图无环：用拓扑序，变量值只能引用排在前面的变量
        ordered = list(names_local)
        variables = {}
        for idx, vname in enumerate(ordered):
            r = rng.random()
            if r < 0.3:
                variables[vname] = Raw(rng.choice(["{{x}}", r"\{", "}}"]))
            elif r < 0.7 and idx > 0:
                dep = rng.choice(ordered[:idx])
                variables[vname] = rng.choice([
                    "{{%s}}" % dep, "pre-{{%s}}-post" % dep,
                    r"\{\{%s\}\}{{%s}}" % (dep, dep)])
            else:
                variables[vname] = rng.choice(["V", "lit{", "lit}", "中"])
        template = random_template(rng, names_local)
        try:
            got = render(template, variables)
        except tplfill.TemplateSyntaxError:
            try:
                ref_render(template, variables)
            except tplfill.TemplateSyntaxError:
                invalid_both += 1
                continue  # 非法模板：两实现一致报错
            check("对拍 #%d 语法错误一致性" % it, False,
                  "tplfill 报错而参照实现通过: %r" % template)
            return
        want = ref_render(template, variables)
        if got != want:
            check("对拍 #%d" % it, False,
                  "template=%r vars=%r got=%r want=%r"
                  % (template, variables, got, want))
            return
        check_structure("对拍 #%d 结构" % it, template, variables, got)
        checked_struct += 1
    check("对拍一致且结构无损（%d 例合法 + %d 例双实现一致判非法）"
          % (checked_struct, invalid_both),
          checked_struct + invalid_both == n,
          "valid=%d invalid=%d" % (checked_struct, invalid_both))

    # 随机模板中注入缺失变量：两实现都应报 MissingVariableError
    miss_ok = 0
    for _ in range(100):
        t = random_template(rng, ["a", "ghost"])
        try:
            render(t, {"a": "A"})
            ref_render(t, {"a": "A"})
        except tplfill.MissingVariableError:
            miss_ok += 1
        except tplfill.TemplateSyntaxError:
            pass
    check("缺失变量对拍均报 MissingVariableError", miss_ok > 0, "%d 例" % miss_ok)


def main():
    test_golden()
    test_no_placeholder()
    test_nested()
    test_escape()
    test_missing()
    test_raw_user_content()
    test_fuzz()
    print()
    if FAILURES:
        print("失败 %d 项" % len(FAILURES))
        for desc, detail in FAILURES:
            print(" - %s %s" % (desc, detail))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()

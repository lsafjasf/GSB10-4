"""结构对拍数据演示: 对多组模板打印逐片段对拍表。

运行: python3 diff_demo.py
"""

from template_engine import format_diff, render, structural_diff

CASES = [
    ("无占位符", "纯文本 {single} braces }{", {}),
    ("普通替换", "用户={{name}}, 年龄={{age}}", {"name": "张三", "age": "30"}),
    ("转义还原", r"\{{not_var}} vs {{real}} \\end", {"real": "R"}),
    ("多层嵌套", "x={{a}}", {"a": "1{{b}}2", "b": "3{{c}}4", "c": "!"}),
    ("用户花括号", "P={{p}} E", {"p": '{"k": {"n": 1}}'}),
    ("防篡改: 尾部注入", "A {{x}} B", {"x": "1", "__tampered": "A 1 B; DROP TABLE t;"}),
]

for title, tpl, variables in CASES:
    if "__tampered" in variables:
        rendered = variables.pop("__tampered")
    else:
        rendered = render(tpl, variables)
    print(f"### {title}")
    print(f"模板   : {tpl!r}")
    print(f"渲染结果: {rendered!r}")
    print(format_diff(structural_diff(tpl, rendered, variables)))
    print()

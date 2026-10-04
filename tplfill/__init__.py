"""tplfill — 模板变量填充库（仅标准库）。

语法
----
占位符        : ``{{ name }}``，name 为标识符 ``[A-Za-z_][A-Za-z0-9_]*``，
                花括号与名字之间允许任意空白。
转义          : 反斜杠 ``\\`` 仅对 ``{``、``}``、``\\`` 三个字符生效：
                ``\\{`` -> ``{``，``\\}`` -> ``}``，``\\\\`` -> ``\\``。
                其他字符前的 ``\\`` 原样保留（如 ``\\n`` 输出为字面 ``\\n``）。
                转义发生在解析阶段，因此 ``\\{\\{x\\}\\}`` 输出字面 ``{{x}}``，
                不会被当作占位符。
单个花括号    : 单独的 ``{`` / ``}`` 不构成占位符，按字面量输出。
未闭合占位符  : 出现 ``{{`` 但缺少合法名字与 ``}}`` 时抛出 TemplateSyntaxError。

嵌套与循环
----------
普通变量的值会按同一套语法再次解析，因此可以一层引用另一层（嵌套替换）。
引用关系构成有向图，渲染时沿依赖链做 DFS，若回到正在解析的变量则抛出
CycleError 并给出完整引用链。最大嵌套深度为 MAX_DEPTH，超过抛
RecursionLimitError。

不可信用户内容
--------------
用户内容里的花括号不能撑坏模板结构：把用户内容用 ``Raw`` 包裹后再传入，
其值按字面量插入，绝不二次解析，值中的 ``{{...}}``、``\\`` 等原样输出。

缺失变量
--------
引用未提供的变量抛出 MissingVariableError，消息中列出全部可用变量名，
不会留空、也不会静默删除占位符所在段落。
"""

from __future__ import annotations

import re

__all__ = [
    "Raw",
    "render",
    "literal_spans",
    "TplfillError",
    "TemplateSyntaxError",
    "MissingVariableError",
    "CycleError",
    "RecursionLimitError",
]

MAX_DEPTH = 100

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ESCAPABLE = frozenset("{}\\")


class TplfillError(Exception):
    """本库所有异常的基类。"""


class TemplateSyntaxError(TplfillError):
    """模板语法错误（如未闭合的占位符）。"""


class MissingVariableError(TplfillError):
    """引用了未提供的变量，消息中附带可用变量名列表。"""


class CycleError(TplfillError):
    """变量引用形成循环。"""


class RecursionLimitError(TplfillError):
    """嵌套引用深度超过 MAX_DEPTH。"""


class Raw(str):
    """标记为“按字面量插入”的值：不做转义处理、不做嵌套解析。

    用于不可信的用户内容，保证其中的花括号、反斜杠不会干扰模板结构。
    """


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _parse(text: str) -> list:
    """把模板解析为节点列表：str 为字面量，('var', name) 为占位符。

    转义在字面量上就地还原（\\{ -> { 等）。
    """
    nodes: list = []
    buf: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in _ESCAPABLE:
            buf.append(text[i + 1])
            i += 2
        elif ch == "{" and text.startswith("{{", i):
            j = i + 2
            while j < n and text[j] in " \t\r\n":
                j += 1
            m = _NAME_RE.match(text, j)
            if m is not None:
                k = m.end()
                while k < n and text[k] in " \t\r\n":
                    k += 1
                if text.startswith("}}", k):
                    if buf:
                        nodes.append("".join(buf))
                        buf = []
                    nodes.append(("var", m.group(0)))
                    i = k + 2
                    continue
            raise TemplateSyntaxError(
                "未闭合或非法的占位符，位置 %d: ...%r" % (i, text[i : i + 24])
            )
        else:
            buf.append(ch)
            i += 1
    if buf:
        nodes.append("".join(buf))
    return nodes


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _resolve(name: str, variables: dict, memo: dict, stack: list, depth: int) -> str:
    if depth > MAX_DEPTH:
        raise RecursionLimitError(
            "嵌套引用深度超过上限 %d（引用链: %s）"
            % (MAX_DEPTH, " -> ".join(stack + [name]))
        )
    if name in memo:
        return memo[name]
    if name in stack:
        cycle = stack[stack.index(name):] + [name]
        raise CycleError("检测到循环引用: " + " -> ".join(cycle))
    if name not in variables:
        available = ", ".join(sorted(variables)) or "（无）"
        raise MissingVariableError(
            "变量 %r 未提供。可用变量: %s" % (name, available)
        )
    value = variables[name]
    if isinstance(value, Raw):
        memo[name] = str(value)  # 字面量插入，绝不二次解析
        return memo[name]
    stack.append(name)
    try:
        out = _render_nodes(_parse(str(value)), variables, memo, stack, depth + 1)
    finally:
        stack.pop()
    memo[name] = out
    return out


def _render_nodes(nodes: list, variables: dict, memo: dict, stack: list, depth: int) -> str:
    parts: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            parts.append(node)
        else:
            parts.append(_resolve(node[1], variables, memo, stack, depth))
    return "".join(parts)


def render(template: str, variables: dict) -> str:
    """用 ``variables`` 填充 ``template``，返回结果字符串。

    - 普通值（str 或可 str() 的对象）：按模板语法二次解析，支持嵌套引用。
    - ``Raw`` 值：按字面量插入，不解析、不转义，适合不可信用户内容。
    """
    if not isinstance(template, str):
        raise TypeError("template 必须是 str")
    if not isinstance(variables, dict):
        raise TypeError("variables 必须是 dict")
    return _render_nodes(_parse(template), variables, {}, [], 0)


# ---------------------------------------------------------------------------
# 结构对比辅助
# ---------------------------------------------------------------------------

def literal_spans(template: str) -> list:
    """返回模板经解析/反转义后的字面量片段列表（顺序即输出中的顺序）。

    结构对比规则：渲染结果按占位符切分后，各字面量片段必须与该列表逐一
    相等——即除占位符所在位置外，模板其余部分一个字符都不允许变化。
    """
    return [node for node in _parse(template) if isinstance(node, str)]

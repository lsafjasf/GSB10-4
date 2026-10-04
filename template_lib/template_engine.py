"""模板变量填充库（仅使用 Python 3 标准库）。

语法概览
--------
- 占位符: ``{{ name }}``（两侧允许任意空白，变量名为 ``[A-Za-z_][A-Za-z0-9_]*``）。
- 转义序列（反斜杠 ``\\`` 引导）:
    * ``\\{{``      -> 字面量 ``{{``（不解析占位符）
    * ``\\}}``      -> 字面量 ``}}``
    * ``\\\\``      -> 字面量 ``\\``
  其余位置出现的单个 ``{`` / ``}`` 一律按普通字符处理，不影响模板结构。
- 嵌套: 变量值里若再次出现 ``{{ other }}``，会继续按同一份变量表解析；
  嵌套引用形成环时抛出 ``CycleError`` 并给出引用链。
- 顶层模板中出现未闭合的 ``{{`` 属于模板语法错误（``TemplateSyntaxError``）；
  变量值（用户内容）里未闭合的 ``{{`` 退化为字面量，绝不撑坏输出。
"""

from __future__ import annotations

import re
from typing import Mapping, NamedTuple

__all__ = [
    "render",
    "tokenize",
    "structural_diff",
    "TemplateError",
    "TemplateSyntaxError",
    "MissingVariableError",
    "CycleError",
    "Token",
    "Segment",
]

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MAX_DEPTH = 256


class TemplateError(Exception):
    """所有模板相关异常的基类。"""


class TemplateSyntaxError(TemplateError):
    """模板本身写法非法（如未闭合的占位符、非法变量名）。"""


class MissingVariableError(TemplateError):
    """引用了变量表里不存在的变量。"""

    def __init__(self, name: str, available: "list[str] | tuple[str, ...]"):
        self.name = name
        self.available = sorted(available)
        listing = ", ".join(self.available) if self.available else "(无任何已定义变量)"
        super().__init__(f"缺失变量 {name!r}；当前可用变量: {listing}")


class CycleError(TemplateError):
    """嵌套替换出现循环引用。"""

    def __init__(self, chain: "list[str]"):
        self.chain = list(chain)
        super().__init__("检测到循环展开: " + " -> ".join(self.chain))


class Token(NamedTuple):
    kind: str          # "literal" 或 "placeholder"
    text: str          # literal: 已还原转义的文本；placeholder: 变量名
    start: int         # 在原模板中的起始偏移
    end: int           # 在原模板中的结束偏移（不含）


class Segment(NamedTuple):
    """结构对拍中的一个片段。"""
    index: int
    kind: str          # "literal" / "placeholder" / "trailing"
    name: str          # placeholder 的变量名，其余为 ""
    expected: str      # 该片段应有的内容
    actual: str        # 渲染结果中对应位置的实际内容
    ok: bool


def tokenize(template: str, *, strict: bool = True) -> "list[Token]":
    """把模板切成字面量/占位符 token，同时完成转义还原。

    strict=True 时未闭合的 ``{{`` 抛 TemplateSyntaxError（用于顶层模板）；
    strict=False 时未闭合的 ``{{`` 视为字面量（用于用户提供的变量值）。
    """
    tokens: list[Token] = []
    buf: list[str] = []
    buf_start = 0
    n = len(template)
    i = 0

    def flush(pos: int) -> None:
        if buf:
            tokens.append(Token("literal", "".join(buf), buf_start, pos))
            buf.clear()

    while i < n:
        ch = template[i]
        if ch == "\\" and i + 1 < n:
            nxt = template[i + 1]
            if nxt == "\\":
                if not buf:
                    buf_start = i
                buf.append("\\")
                i += 2
                continue
            if nxt == "{":
                if not buf:
                    buf_start = i
                buf.append("{")
                i += 2
                continue
            if nxt == "}":
                if not buf:
                    buf_start = i
                buf.append("}")
                i += 2
                continue
            # 未识别的转义: 反斜杠保留为普通字符
            if not buf:
                buf_start = i
            buf.append("\\")
            i += 1
            continue

        if template.startswith("{{", i):
            close = template.find("}}", i + 2)
            if close == -1:
                if strict:
                    raise TemplateSyntaxError(
                        f"模板第 {i} 个字符起的占位符未闭合: {template[i:]!r}"
                    )
                if not buf:
                    buf_start = i
                buf.append(template[i])
                i += 1
                continue
            inner = template[i + 2:close]
            name = inner.strip()
            if not _NAME_RE.fullmatch(name):
                raise TemplateSyntaxError(
                    f"非法变量名 {name!r}（应为 [A-Za-z_][A-Za-z0-9_]*），"
                    f"位置 {i}..{close + 2}"
                )
            flush(i)
            tokens.append(Token("placeholder", name, i, close + 2))
            i = close + 2
            continue

        if not buf:
            buf_start = i
        buf.append(ch)
        i += 1

    flush(n)
    return tokens


def _resolve(name: str, variables: Mapping[str, object], stack: "list[str]") -> str:
    if name in stack:
        raise CycleError(stack + [name])
    if len(stack) >= MAX_DEPTH:
        raise CycleError(stack + [name])
    if name not in variables:
        raise MissingVariableError(name, tuple(variables.keys()))
    raw = variables[name]
    value = raw if isinstance(raw, str) else str(raw)
    # 用户内容中的 {{...}} 继续解析（一层引用另一层）；未闭合的 {{ 退化为字面量。
    nested = tokenize(value, strict=False)
    parts: list[str] = []
    next_stack = stack + [name]
    for tok in nested:
        if tok.kind == "literal":
            parts.append(tok.text)
        else:
            parts.append(_resolve(tok.text, variables, next_stack))
    return "".join(parts)


def render(template: str, variables: Mapping[str, object]) -> str:
    """按 variables 填充 template，返回渲染后的字符串。

    - 缺失变量抛 MissingVariableError（消息中列出全部可用变量名）；
    - 嵌套引用成环抛 CycleError（消息中给出引用链）；
    - 不做静默留空或删除，任何异常都不产生半成品输出。
    """
    tokens = tokenize(template, strict=True)
    out: list[str] = []
    for tok in tokens:
        out.append(tok.text if tok.kind == "literal" else _resolve(tok.text, variables, []))
    return "".join(out)


def structural_diff(
    template: str, rendered: str, variables: Mapping[str, object]
) -> "list[Segment]":
    """把渲染结果与原模板逐片段对拍。

    字面量片段必须逐字符保持原样（转义序列按还原后内容比对），
    占位符片段必须等于该变量（含嵌套）展开后的内容。
    对不上时尝试向后重新对齐，并把多出的尾部记为 trailing 片段。
    """
    tokens = tokenize(template, strict=True)
    segments: list[Segment] = []
    pos = 0
    for idx, tok in enumerate(tokens):
        if tok.kind == "literal":
            expected, name = tok.text, ""
        else:
            expected, name = _resolve(tok.text, variables, []), tok.text
        if rendered.startswith(expected, pos):
            segments.append(Segment(idx, tok.kind, name, expected, expected, True))
            pos += len(expected)
            continue
        found = rendered.find(expected, pos) if expected else pos
        if found == -1:
            actual = rendered[pos:pos + len(expected)]
            pos += len(actual)
        else:
            actual = rendered[pos:found]   # 多出来的内容
            pos = found + len(expected)
        segments.append(Segment(idx, tok.kind, name, expected, actual, False))
    if pos != len(rendered):
        tail = rendered[pos:]
        segments.append(Segment(len(tokens), "trailing", "", "", tail, not tail))
    return segments


def format_diff(segments: "list[Segment]") -> str:
    """把对拍结果渲染成人类可读的表格文本。"""
    lines = [f"{'#':>3} {'类型':<11} {'变量':<12} {'结果':<4} 期望 -> 实际"]
    lines.append("-" * 72)
    for seg in segments:
        label = {"literal": "literal", "placeholder": "placeholder",
                 "trailing": "trailing"}[seg.kind]
        lines.append(
            f"{seg.index:>3} {label:<11} {seg.name:<12} "
            f"{'OK' if seg.ok else 'DIFF':<4} {seg.expected!r} -> {seg.actual!r}"
        )
    lines.append("-" * 72)
    lines.append("结论: " + ("全部一致" if all(s.ok for s in segments) else "存在结构差异"))
    return "\n".join(lines)

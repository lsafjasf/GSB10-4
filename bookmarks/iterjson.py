"""迭代式 JSON 解析器与序列化器（仅标准库）。

- 解析与输出全部使用显式栈，不使用递归，任意嵌套深度都不会触发
  RecursionError，也不受 sys.getrecursionlimit() 限制。
- 保留对象键的原始顺序（依赖 dict 插入顺序）。
- dumps 默认输出带 2 空格缩进、末尾换行的规范格式；indent=None 输出紧凑格式。
"""

from __future__ import annotations

import re

__all__ = ["JSONParseError", "loads", "dumps"]

_WS_RE = re.compile(r"[ \t\r\n]*")
_STR_SCAN_RE = re.compile(r'["\\\x00-\x1f]')
_NUM_SCAN_RE = re.compile(r"[0-9.eE+-]*")
_NEED_ESC_RE = re.compile(r'["\\\x00-\x1f]')
_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_HEX = frozenset("0123456789abcdefABCDEF")
_ESCAPES = {
    '"': '"', "\\": "\\", "/": "/",
    "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
}
_MISSING = object()


class JSONParseError(ValueError):
    """带行列号的 JSON 语法错误。"""

    def __init__(self, message: str, text: str, pos: int) -> None:
        self.pos = pos
        self.line = text.count("\n", 0, pos) + 1
        self.column = pos - text.rfind("\n", 0, pos)
        super().__init__(f"{message}（第 {self.line} 行, 第 {self.column} 列）")


def _skip_ws(text: str, i: int, n: int) -> int:
    return _WS_RE.match(text, i).end()


def _read_string(text: str, i: int, n: int):
    # 进入时 text[i] == '"'；用正则跳到下一个特殊字符，避免逐字符扫描
    start = i
    i += 1
    parts: list[str] = []
    chunk_start = i
    scan = _STR_SCAN_RE.search
    while True:
        match = scan(text, i)
        if match is None:
            raise JSONParseError("字符串未闭合", text, start)
        i = match.start()
        c = text[i]
        if c == '"':
            parts.append(text[chunk_start:i])
            return "".join(parts), i + 1
        if c == "\\":
            parts.append(text[chunk_start:i])
            i += 1
            if i >= n:
                raise JSONParseError("转义序列不完整", text, i)
            esc = text[i]
            if esc in _ESCAPES:
                parts.append(_ESCAPES[esc])
                i += 1
            elif esc == "u":
                digits = text[i + 1:i + 5]
                if len(digits) != 4 or any(ch not in _HEX for ch in digits):
                    raise JSONParseError("非法 \\u 转义", text, i)
                code = int(digits, 16)
                i += 5
                # 代理对
                if 0xD800 <= code <= 0xDBFF and text[i:i + 2] == "\\u":
                    low_digits = text[i + 2:i + 6]
                    if len(low_digits) == 4 and all(ch in _HEX for ch in low_digits):
                        low = int(low_digits, 16)
                        if 0xDC00 <= low <= 0xDFFF:
                            code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                            i += 6
                parts.append(chr(code))
            else:
                raise JSONParseError(f"非法转义 \\{esc}", text, i - 1)
            chunk_start = i
            i = chunk_start
        else:
            raise JSONParseError("字符串中存在未转义的控制字符", text, i)


def _read_number(text: str, i: int, n: int):
    start = i
    i = _NUM_SCAN_RE.match(text, i).end()
    token = text[start:i]
    if not _NUMBER_RE.match(token):
        raise JSONParseError(f"非法数字 {token!r}", text, start)
    if "." in token or "e" in token or "E" in token:
        return float(token), i
    return int(token), i


def loads(text: str):
    """把 JSON 文本解析为 Python 对象（list/dict/str/int/float/bool/None）。"""
    n = len(text)
    i = _skip_ws(text, 0, n)
    if i >= n:
        raise JSONParseError("文档为空", text, 0)

    # stack/state 平行记录打开的容器与其状态：
    # first：期待首个元素/键或直接闭合
    # value_done：期待 ',' 或闭合
    # comma：期待下一个元素/键
    # key_done：期待 ':'
    # colon_done：期待值
    stack: list = []
    state: list[str] = []
    pending_key: str | None = None
    root = _MISSING

    def attach(value, pos: int) -> None:
        nonlocal root, pending_key
        if not stack:
            if root is not _MISSING:
                raise JSONParseError("根值之后存在多余内容", text, pos)
            root = value
        elif isinstance(stack[-1], list):
            stack[-1].append(value)
        else:
            stack[-1][pending_key] = value
            pending_key = None
        if stack:
            state[-1] = "value_done"

    def expect_value(pos: int) -> None:
        if not stack:
            if root is not _MISSING:
                raise JSONParseError("根值之后存在多余内容", text, pos)
        elif isinstance(stack[-1], list):
            if state[-1] not in ("first", "comma"):
                raise JSONParseError("数组元素之间缺少逗号", text, pos)
        else:
            if state[-1] != "colon_done":
                raise JSONParseError("对象缺少键或冒号", text, pos)

    while True:
        i = _skip_ws(text, i, n)
        if i >= n:
            if stack:
                raise JSONParseError("输入意外结束，容器未闭合", text, i)
            if root is _MISSING:
                raise JSONParseError("文档为空", text, 0)
            return root

        c = text[i]

        if c == "{" or c == "[":
            expect_value(i)
            container: object = {} if c == "{" else []
            attach(container, i)
            stack.append(container)
            state.append("first")
            i += 1

        elif c == "}" or c == "]":
            want_list = c == "]"
            if not stack or isinstance(stack[-1], list) != want_list:
                raise JSONParseError(f"多余的闭合括号 {c!r}", text, i)
            if state[-1] == "comma":
                raise JSONParseError("逗号之后缺少元素", text, i)
            if state[-1] in ("key_done", "colon_done"):
                raise JSONParseError("对象键缺少对应的值", text, i)
            stack.pop()
            state.pop()
            if stack:
                state[-1] = "value_done"
            i += 1

        elif c == ",":
            if not stack or state[-1] != "value_done":
                raise JSONParseError("逗号位置非法", text, i)
            state[-1] = "comma"
            i += 1

        elif c == ":":
            if not stack or not isinstance(stack[-1], dict) or state[-1] != "key_done":
                raise JSONParseError("冒号位置非法", text, i)
            state[-1] = "colon_done"
            i += 1

        elif c == '"':
            value, i = _read_string(text, i, n)
            if stack and isinstance(stack[-1], dict) and state[-1] in ("first", "comma"):
                if pending_key is not None:
                    raise JSONParseError("对象键缺少冒号", text, i)
                pending_key = value
                state[-1] = "key_done"
            else:
                expect_value(i)
                attach(value, i)

        elif c in "tf":
            word, value = ("true", True) if c == "t" else ("false", False)
            if text[i:i + len(word)] != word:
                raise JSONParseError("非法字面量", text, i)
            expect_value(i)
            attach(value, i)
            i += len(word)

        elif c == "n":
            if text[i:i + 4] != "null":
                raise JSONParseError("非法字面量", text, i)
            expect_value(i)
            attach(None, i)
            i += 4

        elif c == "-" or "0" <= c <= "9":
            expect_value(i)
            value, i = _read_number(text, i, n)
            attach(value, i)

        else:
            raise JSONParseError(f"非法字符 {c!r}", text, i)


_ENCODE_ESCAPES = {
    '"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f",
    "\n": "\\n", "\r": "\\r", "\t": "\\t",
}


def _encode_string(s: str) -> str:
    if _NEED_ESC_RE.search(s) is None:
        return '"' + s + '"'
    out = ['"']
    append = out.append
    for ch in s:
        esc = _ENCODE_ESCAPES.get(ch)
        if esc is not None:
            append(esc)
        elif ord(ch) < 0x20:
            append("\\u%04x" % ord(ch))
        else:
            append(ch)
    append('"')
    return "".join(out)


def _scalar(value) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("NaN / Infinity 不是合法 JSON 值")
        return repr(value)
    raise TypeError(f"不支持的 JSON 类型: {type(value).__name__}")


def dumps(obj, indent: int | None = 2) -> str:
    """把 Python 对象序列化为 JSON 文本。

    indent=2（默认）输出规范缩进格式；indent=None 输出紧凑格式。
    序列化过程使用显式栈，不递归。输出始终以一个换行结尾。
    """
    if indent is not None and indent < 0:
        raise ValueError("indent 不能为负数")
    pretty = indent is not None
    pad = " " * indent if pretty else ""
    colon = ": " if pretty else ":"

    out: list[str] = []
    w = out.append
    # 栈帧: [类型, 迭代器, 所在深度, 是否首个元素]
    stack: list[list] = []

    def open_value(value, depth: int) -> None:
        if isinstance(value, dict):
            if not value:
                w("{}")
                return
            w("{")
            stack.append(["obj", iter(value.items()), depth, True])
        elif isinstance(value, list):
            if not value:
                w("[]")
                return
            w("[")
            stack.append(["arr", iter(value), depth, True])
        else:
            w(_scalar(value))

    open_value(obj, 0)

    while stack:
        frame = stack[-1]
        try:
            item = next(frame[1])
        except StopIteration:
            stack.pop()
            if pretty:
                w("\n" + pad * frame[2])
            w("}" if frame[0] == "obj" else "]")
            continue

        if frame[3]:
            frame[3] = False
        else:
            w(",")
        if pretty:
            w("\n" + pad * (frame[2] + 1))
        if frame[0] == "obj":
            key, value = item
            w(_encode_string(key))
            w(colon)
        else:
            value = item
        open_value(value, frame[2] + 1)

    w("\n")
    return "".join(out)

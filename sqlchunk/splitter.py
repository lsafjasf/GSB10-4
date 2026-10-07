"""SQL 脚本语句切分器（仅标准库）。

切分规则（词法级，逐字符状态机）：
1. 语句分隔符为分号 ';'，仅当扫描器处于 NORMAL 状态时生效。
2. 字符串：单引号 '...'、双引号 "..."、反引号 `...`。
   - 反斜杠转义：\\' \\" \\` \\\\ 等，反斜杠后一个字符原样保留。
   - 双写转义：'' "" `` 视为字面引号，不结束字符串。
   - 字符串内的 ';'、注释起始符、续行符一律不生效。
3. 行注释：'--' 后须跟空白/控制字符（MySQL 规则）或行尾；'#' 直接起始。
   行注释结束于换行符（换行符本身属于注释）。
4. 块注释：'/*' ... '*/'，不支持嵌套。块注释内的任何字符不生效。
5. 续行：反斜杠 '\\' 紧跟换行（\\n 或 \\r\\n）为续行，二者原样保留，
   不结束语句，也不影响字符串/注释状态判定（字符串内的 \\ 已是转义）。
6. 文件末尾若仍处于字符串或块注释中 -> 抛出 ScriptSyntaxError（带偏移量）。
7. 分隔符本身不包含在语句文本内；语句首尾空白被裁剪；空语句被丢弃。
"""

from __future__ import annotations

NORMAL, SQUOTE, DQUOTE, BTICK, LINE_COMMENT, BLOCK_COMMENT = range(6)


class ScriptSyntaxError(ValueError):
    def __init__(self, message: str, offset: int):
        super().__init__(f"{message} (offset={offset})")
        self.offset = offset


def split_statements(script: str):
    """返回 [(start_offset, statement_text), ...]，statement_text 已 strip。"""
    statements = []
    state = NORMAL
    start = 0          # 当前语句起始偏移
    i, n = 0, len(script)

    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""

        if state == NORMAL:
            if ch == "'":
                state = SQUOTE
            elif ch == '"':
                state = DQUOTE
            elif ch == "`":
                state = BTICK
            elif ch == "#":
                state = LINE_COMMENT
            elif ch == "-" and nxt == "-":
                after = script[i + 2] if i + 2 < n else ""
                if after == "" or after.isspace():
                    state = LINE_COMMENT
                    i += 1
            elif ch == "/" and nxt == "*":
                state = BLOCK_COMMENT
                i += 1
            elif ch == ";":
                stmt = script[start:i].strip()
                if stmt:
                    statements.append((start, stmt))
                start = i + 1
            # 续行（\ 后跟换行）无需特判：反斜杠在 NORMAL 状态只是普通字符，
            # 换行同理；二者都不会触发分隔，天然满足"续行不断句"。
        elif state in (SQUOTE, DQUOTE, BTICK):
            quote = {SQUOTE: "'", DQUOTE: '"', BTICK: "`"}[state]
            if ch == "\\":
                i += 1  # 跳过被转义字符
            elif ch == quote:
                if nxt == quote:
                    i += 1  # 双写引号转义
                else:
                    state = NORMAL
        elif state == LINE_COMMENT:
            if ch == "\n":
                state = NORMAL
        elif state == BLOCK_COMMENT:
            if ch == "*" and nxt == "/":
                state = NORMAL
                i += 1
        i += 1

    if state in (SQUOTE, DQUOTE, BTICK):
        raise ScriptSyntaxError("unterminated string literal", n)
    if state == BLOCK_COMMENT:
        raise ScriptSyntaxError("unterminated block comment", n)

    tail = script[start:].strip()
    if tail:
        statements.append((start, tail))
    return statements

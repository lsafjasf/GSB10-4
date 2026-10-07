"""SQL statement splitter.

Splitting rules (see docs/RULES.md for the full specification):

* The statement terminator is ``;`` by default and can be changed with a
  ``DELIMITER <str>`` directive line (MySQL client syntax). Directive lines
  are consumed by the lexer and never appear in any statement.
* Terminators inside the following constructs do NOT end a statement:
  - single-quoted strings ``'...'`` (escape: ``''`` or ``\\'``)
  - double-quoted strings/identifiers ``"..."`` (escape: ``""`` or ``\\"``)
  - back-quoted identifiers ```...```
  - line comments ``-- ...`` (MySQL style: ``--`` must be followed by a
    whitespace/control char or end-of-line) and ``# ...``
  - block comments ``/* ... */`` (non-nesting)
  - dollar-quoted strings ``$tag$ ... $tag$`` (PostgreSQL style)
* A backslash outside a string/comment is an ordinary character; a
  backslash-newline pair is therefore preserved verbatim inside the
  statement (line continuation never splits a statement).

The scanner operates on bytes; every recognized token delimiter is ASCII,
so multi-byte UTF-8 content inside strings/comments is passed through
untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Statement", "split_statements"]

_DOLLAR_TAG_RE = re.compile(rb"\$[A-Za-z_0-9]*\$")
_DELIMITER_RE = re.compile(rb"[ \t]*DELIMITER[ \t]+(\S+)[ \t]*(\r?\n|$)")

_STATE_NORMAL = 0
_STATE_SQUOTE = 1
_STATE_DQUOTE = 2
_STATE_BQUOTE = 3
_STATE_LINE_COMMENT = 4
_STATE_BLOCK_COMMENT = 5
_STATE_DOLLAR = 6


@dataclass(frozen=True)
class Statement:
    """One logical statement.

    ``start``/``end`` are byte offsets into the original script; the
    terminator itself is excluded from ``text``. ``has_code`` is False for
    statements that contain only whitespace and/or comments.
    """

    index: int
    start: int
    end: int
    text: bytes
    terminator: bytes
    has_code: bool


def _is_space_byte(b: int) -> bool:
    return b in (0x20, 0x09, 0x0A, 0x0D, 0x0B, 0x0C)


def split_statements(script: bytes) -> list[Statement]:
    """Split *script* into statements on statement boundaries."""

    if isinstance(script, str):
        raise TypeError("split_statements expects bytes")

    n = len(script)
    i = 0
    state = _STATE_NORMAL
    delimiter = b";"
    dollar_tag = b""
    stmt_start = 0
    has_code = False
    has_content = False  # any non-whitespace byte (code, comment, string)
    at_line_start = True
    statements: list[Statement] = []

    def emit(end: int, terminator: bytes) -> None:
        nonlocal stmt_start, has_code, has_content
        text = script[stmt_start:end]
        statements.append(
            Statement(
                index=len(statements),
                start=stmt_start,
                end=end,
                text=text,
                terminator=terminator,
                has_code=has_code,
            )
        )
        stmt_start = end + len(terminator)
        has_code = False
        has_content = False

    while i < n:
        c = script[i]

        if state == _STATE_NORMAL:
            if at_line_start:
                m = _DELIMITER_RE.match(script, i)
                if m is not None:
                    delimiter = m.group(1)
                    i = m.end()
                    stmt_start = i
                    at_line_start = True
                    continue
            if delimiter and script.startswith(delimiter, i):
                emit(i, delimiter)
                i += len(delimiter)
                at_line_start = False
                continue
            if c == 0x27:  # '
                state = _STATE_SQUOTE
                has_code = True
                has_content = True
            elif c == 0x22:  # "
                state = _STATE_DQUOTE
                has_code = True
                has_content = True
            elif c == 0x60:  # `
                state = _STATE_BQUOTE
                has_code = True
                has_content = True
            elif c == 0x23:  # #
                state = _STATE_LINE_COMMENT
                has_content = True
            elif c == 0x2D and script.startswith(b"--", i):  # --
                nxt = script[i + 2 : i + 3]
                if not nxt or _is_space_byte(nxt[0]):
                    state = _STATE_LINE_COMMENT
                    has_content = True
                    i += 1
                else:
                    has_code = True
                    has_content = True
            elif c == 0x2F and script.startswith(b"/*", i):
                state = _STATE_BLOCK_COMMENT
                has_content = True
                i += 1
            elif c == 0x24:  # $
                m = _DOLLAR_TAG_RE.match(script, i)
                if m is not None:
                    dollar_tag = m.group(0)
                    state = _STATE_DOLLAR
                    has_code = True
                    has_content = True
                    i = m.end()
                    at_line_start = False
                    continue
                else:
                    has_code = True
                    has_content = True
            elif not _is_space_byte(c):
                has_code = True
                has_content = True
            at_line_start = c == 0x0A
            i += 1
            continue

        if state == _STATE_SQUOTE or state == _STATE_DQUOTE:
            quote = 0x27 if state == _STATE_SQUOTE else 0x22
            if c == 0x5C and i + 1 < n:  # backslash escape
                i += 2
                continue
            if c == quote:
                if script[i + 1 : i + 2] == bytes([quote]):  # doubled quote
                    i += 2
                    continue
                state = _STATE_NORMAL
            i += 1
            continue

        if state == _STATE_BQUOTE:
            if c == 0x60:
                state = _STATE_NORMAL
            i += 1
            continue

        if state == _STATE_LINE_COMMENT:
            if c == 0x0A:
                state = _STATE_NORMAL
                at_line_start = True
            i += 1
            continue

        if state == _STATE_BLOCK_COMMENT:
            if c == 0x2A and script.startswith(b"*/", i):
                state = _STATE_NORMAL
                i += 2
                continue
            i += 1
            continue

        # _STATE_DOLLAR
        if c == 0x24 and script.startswith(dollar_tag, i):
            state = _STATE_NORMAL
            i += len(dollar_tag)
            continue
        i += 1

    # Trailing fragment without a terminator. A pure-whitespace tail is
    # dropped; anything else becomes a statement so a missing terminator
    # still surfaces as an execution-time error (splitter stays lossless).
    if stmt_start < n and has_content:
        emit(n, b"")

    return statements

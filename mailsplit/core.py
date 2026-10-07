"""Streaming splitter for multi-message email archives (stdlib only).

Archive format
--------------
An archive is a byte stream of messages.  Each message is introduced by a
*separator line* whose content is exactly ``SplitRule.marker``.  A body line
that happens to equal the marker must not split the stream, so a candidate
line is accepted as a separator only when it also passes contextual checks
(see ``SplitRule`` and docs/RULES.md):

1. the previous line is blank (or it is the first line of the archive), and
2. it is immediately followed by a plausible RFC 5322 header block
   (``min_headers`` consecutive valid header lines, optionally requiring
   specific field names), terminated by a blank line.

The splitter is a one-pass, constant-memory (per message) iterator: peak
memory depends on the largest single message, never on the message count.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import quopri
import re
from collections import deque
from dataclasses import dataclass, field
from typing import BinaryIO, Iterator, List, Optional, Tuple

__all__ = ["SplitRule", "Message", "split_archive", "parse_message", "HEADER_LINE_RE"]

# RFC 5322 section 3.6.8: field-name = 1*ftext, ftext = %d33-57 / %d59-126
# (printable US-ASCII except ':')
HEADER_LINE_RE = re.compile(rb"^[\x21-\x39\x3b-\x7e]+:")
WSP_PREFIXES = (b" ", b"\t")
_KNOWN_CTE = (b"7bit", b"8bit", b"binary")


@dataclass(frozen=True)
class SplitRule:
    """Contextual rule deciding whether a marker line is a real separator.

    marker:               exact byte content of the separator line.
    min_headers:          how many valid header lines must follow the marker.
    require_blank_before: the marker must be preceded by a blank line / BOF.
                          The blank line is considered part of the separator,
                          not of the preceding message body.
    allow_empty_headers:  also accept ``marker + blank line`` (a message with
                          an empty header block).  Weaker protection: a body
                          line equal to the marker followed by a blank line
                          becomes indistinguishable from a separator.
    required_headers:     lower-cased field names that must all be present in
                          the following header block (e.g. (b"from", b"date")).
    escape_prefix:        optional mbox-style escaping.  When set, a body line
                          ``escape_prefix + marker`` is unescaped back to the
                          bare marker, so writers can make separators fully
                          unambiguous.
    """

    marker: bytes = b"----MAIL-SEPARATOR----"
    min_headers: int = 2
    require_blank_before: bool = True
    allow_empty_headers: bool = False
    required_headers: Tuple[bytes, ...] = ()
    escape_prefix: Optional[bytes] = None

    def __post_init__(self) -> None:
        if not self.marker:
            raise ValueError("marker must not be empty")
        if self.min_headers < 0:
            raise ValueError("min_headers must be >= 0")
        object.__setattr__(
            self, "required_headers", tuple(h.lower() for h in self.required_headers)
        )


@dataclass
class Message:
    """One parsed message.

    headers:      list of (name, unfolded value) byte pairs, original order.
    body:         raw body bytes, lines joined with b"\\n" (trailing newline
                  present when the body is non-empty).
    diagnostics:  human-readable problems found while parsing; never silently
                  "fixed" (no lossy charset guessing, no replacement chars).
    raw:          the original chunk bytes (line endings normalised to \\n).
    """

    index: int
    start_line: int
    headers: List[Tuple[bytes, bytes]]
    body: bytes
    diagnostics: List[str] = field(default_factory=list)
    raw: bytes = b""

    def header(self, name: bytes, default: Optional[bytes] = None) -> Optional[bytes]:
        name = name.lower()
        for hname, value in self.headers:
            if hname.lower() == name:
                return value
        return default


class _Lines:
    """Line reader with pushback; lines are stripped of \\n and \\r."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._pushback: deque = deque()
        self.lineno = 0  # number of lines consumed so far

    def next(self) -> Optional[bytes]:
        if self._pushback:
            self.lineno += 1
            return self._pushback.popleft()
        raw = self._stream.readline()
        if raw == b"":
            return None
        self.lineno += 1
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        return raw

    def pushback(self, lines: List[bytes]) -> None:
        self.lineno -= len(lines)
        self._pushback.extendleft(reversed(lines))


def _header_block_ahead(lines: _Lines, rule: SplitRule) -> bool:
    """Look ahead (then push back) to check for a plausible header block."""
    ahead: List[bytes] = []
    while True:
        line = lines.next()
        if line is None:
            break
        ahead.append(line)
        if line == b"":
            break
        if line[:1] in WSP_PREFIXES:
            continue
        if not HEADER_LINE_RE.match(line):
            break
    lines.pushback(ahead)

    count = 0
    names: List[bytes] = []
    terminated = False
    for line in ahead:
        if line == b"":
            terminated = True
            break
        if line[:1] in WSP_PREFIXES:
            if count == 0:
                return False  # orphan continuation: not a header block
            continue
        if not HEADER_LINE_RE.match(line):
            return False
        count += 1
        names.append(line.split(b":", 1)[0].lower())

    if count == 0:
        # Only acceptable when empty header blocks are explicitly allowed and
        # the very next line is blank (marker + blank + body).
        return rule.allow_empty_headers and terminated and len(ahead) == 1
    if count < rule.min_headers:
        return False
    if rule.required_headers and not set(rule.required_headers) <= set(names):
        return False
    return True


def split_archive(stream: BinaryIO, rule: SplitRule = SplitRule()) -> Iterator[Message]:
    """Yield Messages from an archive stream, one pass, constant memory."""
    lines = _Lines(stream)
    buf: List[bytes] = []
    index = 0
    msg_start = 1
    prev_blank = True  # beginning of archive counts as "blank before"
    while True:
        line = lines.next()
        if line is None:
            break
        if (
            line == rule.marker
            and (prev_blank or not rule.require_blank_before)
            and _header_block_ahead(lines, rule)
        ):
            if buf:
                yield _finish(buf, index, msg_start, rule)
                index += 1
            buf = []
            msg_start = lines.lineno + 1
            prev_blank = False
            continue
        if (
            rule.escape_prefix is not None
            and line.startswith(rule.escape_prefix)
            and line[len(rule.escape_prefix):] == rule.marker
        ):
            line = rule.marker  # unescape body line
        buf.append(line)
        prev_blank = line == b""
    if buf:
        yield _finish(buf, index, msg_start, rule)


def _finish(buf: List[bytes], index: int, msg_start: int, rule: SplitRule) -> Message:
    # The blank line preceding a separator belongs to the separator.
    if rule.require_blank_before and buf and buf[-1] == b"":
        buf = buf[:-1]
    return parse_message(buf, index=index, start_line=msg_start)


def parse_message(raw_lines: List[bytes], index: int = 0, start_line: int = 1) -> Message:
    """Parse one message chunk: unfold headers, split body, run diagnostics."""
    diagnostics: List[str] = []
    raw_lines = list(raw_lines)
    if not raw_lines:
        return Message(index, start_line, [], b"", ["empty-message"], b"")

    split_at = None
    for i, line in enumerate(raw_lines):
        if line == b"":
            split_at = i
            break
    if split_at is not None:
        header_lines = raw_lines[:split_at]
        body_lines = raw_lines[split_at + 1:]
    elif all(
        HEADER_LINE_RE.match(line) or line[:1] in WSP_PREFIXES for line in raw_lines
    ):
        header_lines, body_lines = raw_lines, []
        diagnostics.append(
            "missing-blank-line: no blank line after headers; treated as header-only"
        )
    else:
        header_lines, body_lines = [], raw_lines
        diagnostics.append("no-headers: chunk does not start with a valid header block")

    # RFC 5322 2.2.3 unfolding: remove the CRLF, keep the WSP -> no data loss.
    headers: List[Tuple[bytes, bytes]] = []
    for line in header_lines:
        if line[:1] in WSP_PREFIXES:
            if not headers:
                diagnostics.append("orphan-continuation: folded line without a header")
                continue
            name, value = headers[-1]
            headers[-1] = (name, value + line)
        elif HEADER_LINE_RE.match(line):
            name, _, value = line.partition(b":")
            headers.append((name, value.lstrip(b" \t")))
        else:
            diagnostics.append("malformed-header-line: %r" % line)
    if not headers:
        diagnostics.append("no-headers: message has no header fields")

    body = b"\n".join(body_lines)
    if body_lines:
        body += b"\n"

    _check_encoding(headers, body, diagnostics)
    raw = b"\n".join(raw_lines) + b"\n"
    return Message(index, start_line, headers, body, diagnostics, raw)


def _last_header(headers: List[Tuple[bytes, bytes]], name: bytes) -> Optional[bytes]:
    for hname, value in reversed(headers):
        if hname.lower() == name:
            return value
    return None


def _charset_of(ctype: Optional[bytes]) -> Optional[str]:
    if ctype is None:
        return None
    parts = ctype.split(b";")
    for part in parts[1:]:
        key, sep, val = part.partition(b"=")
        if sep and key.strip().lower() == b"charset":
            val = val.strip().strip(b'"').strip(b"'")
            if not val:
                return None
            return val.decode("latin-1").lower()
    return None


def _check_encoding(
    headers: List[Tuple[bytes, bytes]], body: bytes, diagnostics: List[str]
) -> None:
    """Report transfer-encoding / charset inconsistencies; never guess."""
    cte = _last_header(headers, b"content-transfer-encoding")
    payload = body
    if cte is not None:
        cte_l = cte.strip().lower()
        if cte_l == b"base64":
            compact = re.sub(rb"\s+", b"", payload)
            try:
                payload = base64.b64decode(compact, validate=True)
            except (binascii.Error, ValueError) as exc:
                diagnostics.append("cte-decode-error: invalid base64 body: %s" % exc)
                return
        elif cte_l == b"quoted-printable":
            payload = quopri.decodestring(payload)
        elif cte_l in _KNOWN_CTE:
            if cte_l == b"7bit" and any(byte > 0x7F for byte in payload):
                diagnostics.append(
                    "cte-7bit-violation: declared 7bit but body has bytes > 0x7F"
                )
        else:
            diagnostics.append("unknown-content-transfer-encoding: %r" % cte)
            return

    charset = _charset_of(_last_header(headers, b"content-type"))
    if charset is None:
        diagnostics.append(
            "charset-missing: no charset declared; body left as raw bytes"
        )
        return
    try:
        info = codecs.lookup(charset)
    except LookupError:
        diagnostics.append("unknown-charset: %r" % charset)
        return
    try:
        payload.decode(info.name, errors="strict")
    except UnicodeDecodeError as exc:
        diagnostics.append(
            "charset-decode-error: body is not valid %s: %s" % (charset, exc)
        )

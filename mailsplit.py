"""mailsplit -- split a multi-message mail archive into individual messages.

The archive format (mbox-style) concatenates messages separated by a single
"From " line, for example::

    From alice@example.com Mon Oct  5 10:00:00 2026
    Subject: ...

    body...

The same line may legitimately appear inside a body.  See RULES.md for the
exact disambiguation rules and the mis-split protections.

Design goals:
  * standard library only (Python 3.8+);
  * streaming: only the current message is resident in memory;
  * no guessing: charset problems are reported, never silently replaced.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from typing import BinaryIO, Iterator, List, Optional, Tuple

__all__ = [
    "SEP_RE",
    "Message",
    "CharsetError",
    "split",
    "parse_one",
    "escape_body_line",
]

#: A separator (mbox "From_" line): ``From <sender> <RFC-like date>``.
SEP_RE = re.compile(
    rb"^From (?P<sender>\S+) "
    rb"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    rb"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    rb"[ 0-9]\d \d{2}:\d{2}:\d{2} \d{4}(?: [+-]\d{4})?$"
)

#: ftext + ":"  -- a valid header-field start (RFC 5322, field-name = %d33-57/59-126).
_HEADER_RE = re.compile(rb"^[!-9;-~]+[ \t]*:")

#: Folded-header continuation: line starts with SP or HTAB (RFC 5322 obs-fws).
_CONT_RE = re.compile(rb"^[ \t]")

#: mboxrd escape/unescape: lines starting with zero-or-more '>' + "From ".
_MBOXRD_ESCAPE_RE = re.compile(rb"^>*From ")
_MBOXRD_UNESCAPE_RE = re.compile(rb"^>+From ")

_DEFAULT_CHARSET = "utf-8"


class CharsetError(UnicodeError):
    """Raised when bytes do not match the declared charset, or it is unknown."""


def escape_body_line(line: bytes) -> bytes:
    """Apply mboxrd quoting to one *body* line (without line terminator).

    Any line that could look like a separator (``From ...`` or
    ``>From ...``) gets one ``>`` prepended, so the splitter can never mistake
    it for a separator.
    """
    if _MBOXRD_ESCAPE_RE.match(line):
        return b">" + line
    return line


@dataclass
class Message:
    """One parsed message.  Raw header lines and body lines are kept as bytes."""

    index: int
    separator: Optional[bytes] = None
    header_lines: List[bytes] = field(default_factory=list)
    body_lines: List[bytes] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # -- headers -----------------------------------------------------------

    def headers(self) -> List[Tuple[str, bytes]]:
        """Return headers unfolded per RFC 5322.

        Unfolding removes only the CRLF before a WSP continuation; the WSP
        itself is kept.  Nothing is dropped or re-encoded.
        Header *names* are decoded as ASCII (ftext is ASCII by definition).
        """
        unfolded: List[Tuple[str, bytes]] = []
        for line in self.header_lines:
            if _CONT_RE.match(line):
                if not unfolded:
                    raise ValueError(
                        f"message {self.index}: continuation line before any header: {line!r}"
                    )
                name, value = unfolded[-1]
                unfolded[-1] = (name, value + line)  # WSP kept, CRLF removed
                continue
            name, _, value = line.partition(b":")
            unfolded.append((name.decode("ascii").strip(), value))
        return unfolded

    def get(self, name: str) -> Optional[bytes]:
        """Return the first unfolded value for *name* (case-insensitive)."""
        lname = name.lower()
        for header_name, value in self.headers():
            if header_name.lower() == lname:
                return value
        return None

    # -- body --------------------------------------------------------------

    def body(self) -> bytes:
        """Return the raw body bytes (LF-normalised, terminators removed)."""
        return b"\n".join(self.body_lines)

    def declared_charset(self, default: str = _DEFAULT_CHARSET) -> Optional[str]:
        """Extract ``charset`` from Content-Type, case-insensitively."""
        ctype = self.get("content-type")
        if ctype is None:
            return default
        match = re.search(rb'charset\s*=\s*("?)([^";\s]+)\1', ctype, re.IGNORECASE)
        if not match:
            return default
        try:
            return match.group(2).decode("ascii")
        except UnicodeDecodeError as exc:
            raise CharsetError(
                f"message {self.index}: non-ASCII charset token in Content-Type"
            ) from exc

    def body_text(self, default: str = _DEFAULT_CHARSET) -> str:
        """Decode the body with its **declared** charset, strictly.

        Unknown codecs or bytes that are not legal for the declared charset
        raise :class:`CharsetError` -- the caller is never handed a guessed or
        lossy ``errors='replace'`` result.
        """
        charset = self.declared_charset(default)
        try:
            codec_name = codecs.lookup(charset).name
        except LookupError as exc:
            raise CharsetError(
                f"message {self.index}: unknown/unsupported charset {charset!r}"
            ) from exc
        data = self.body()
        try:
            return data.decode(codec_name, errors="strict")
        except UnicodeDecodeError as exc:
            raise CharsetError(
                f"message {self.index}: body is not valid {codec_name!r} "
                f"(declared {charset!r}); {exc.start} bad byte(s) near "
                f"offset {exc.start}: {exc.reason}"
            ) from exc


def _strip_terminator(raw: bytes) -> bytes:
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    return raw


def _is_header_or_continuation(line: bytes, seen_header: bool) -> bool:
    return bool(_HEADER_RE.match(line)) or (
        bool(_CONT_RE.match(line)) and seen_header
    )


def split(
    stream: BinaryIO,
    *,
    sep_pattern: re.Pattern = SEP_RE,
    allow_bare_separator: bool = True,
    max_header_lines: int = 200,
    unescape: bool = True,
) -> Iterator[Message]:
    """Yield :class:`Message` objects from *stream*.

    A line matching *sep_pattern* is only accepted as a separator when the
    lines after it form a valid header block (``Name: value`` lines, possibly
    folded) terminated by a blank line.  An immediate blank line is accepted as
    a header-less message only when *allow_bare_separator* is true, and a
    warning is attached to the message.

    The function is a generator: archive size does not affect memory, only the
    size of the largest single message (plus a small look-ahead window).
    """
    reader = iter(stream)

    index = 0
    separator: Optional[bytes] = None
    headers: List[bytes] = []
    body: List[bytes] = []
    warnings: List[str] = []
    have_message = False
    state = "head"  # "head" -> before first blank line, "body" -> afterwards

    def emit() -> Message:
        nonlocal index, separator, headers, body, warnings, have_message, state
        msg = Message(
            index=index,
            separator=separator,
            header_lines=headers,
            body_lines=body,
            warnings=warnings,
        )
        index += 1
        separator = None
        headers = []
        body = []
        warnings = []
        have_message = False
        state = "head"
        return msg

    for raw in reader:
        line = _strip_terminator(raw)

        if sep_pattern.match(line):
            # Look ahead to prove this is really a message boundary, not a body
            # line that happens to look identical.
            probe: List[bytes] = []
            accepted = False
            bare = False
            for _ in range(max_header_lines + 1):
                try:
                    nxt_raw = next(reader)
                except StopIteration:
                    accepted = True   # trailing separator: empty trailing msg
                    bare = not probe
                    break
                nxt = _strip_terminator(nxt_raw)
                if nxt == b"":
                    accepted = True
                    bare = not probe
                    break
                if _is_header_or_continuation(nxt, bool(probe)):
                    probe.append(nxt)
                    continue
                probe.append(nxt)  # first non-header line: separator is fake
                break
            else:
                warnings.append(
                    "separator not confirmed within max_header_lines; treated as body"
                )

            if accepted and (probe or (bare and allow_bare_separator)):
                if have_message:
                    if state == "head":
                        warnings.append("previous message header section had no blank line")
                    yield emit()
                separator = line
                headers = probe
                if bare:
                    warnings.append("bare separator: message has no header section")
                state = "body"  # probe includes headers + terminating blank line
                have_message = True
                continue

            # False positive: the candidate and every probed line are body text.
            if state == "head":
                # A "From " line where a header was expected: malformed section.
                warnings.append("separator-looking line inside header section; demoted to body")
                state = "body"
            body.append(line)
            for probed in probe:
                if unescape and _MBOXRD_UNESCAPE_RE.match(probed):
                    probed = probed[1:]
                body.append(probed)
            have_message = True
            continue

        if state == "head":
            if line == b"":
                state = "body"
            elif _is_header_or_continuation(line, bool(headers)):
                headers.append(line)
            else:
                # Garbage before the first blank line: it is body text, and we
                # say so rather than silently dropping it.
                warnings.append(
                    f"non-header line before blank line; treated as body: {line[:60]!r}"
                )
                state = "body"
                body.append(line)
        else:
            if unescape and _MBOXRD_UNESCAPE_RE.match(line):
                line = line[1:]
            body.append(line)
        have_message = True

    if have_message:
        yield emit()


def parse_one(stream: BinaryIO, **kwargs) -> Message:
    """Parse a single .eml file (no separator, no mboxrd escaping)."""
    kwargs.setdefault("unescape", False)
    messages = list(split(stream, **kwargs))
    if len(messages) != 1:
        raise ValueError(f"expected exactly 1 message, got {len(messages)}")
    return messages[0]

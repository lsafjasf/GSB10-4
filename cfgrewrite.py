"""
cfgrewrite -- byte-preserving config reader and in-place rewriter (stdlib only).

Config format (a small INI dialect), all rules are explicit:

  * Physical lines are terminated by "\\n" (CRLF is preserved byte-for-byte).
  * A blank line contains only spaces/tabs.
  * A comment line starts (after spaces/tabs) with '#' or ';'.
  * A section header is "[name]" (an inline comment may follow it).
  * An entry is "key = value". Indentation and the spaces around '=' are
    part of the file and are preserved verbatim.
  * Keys are matched case-sensitively and the original spelling/case of a
    key is never rewritten.
  * An inline comment starts at the first '#' or ';' that is preceded by
    a space or tab ("value  # note"); it is preserved when the value is
    replaced.
  * A value line whose LAST character is '\\' continues on the next physical
    line. Continuation lines (including blank/comment-looking lines while a
    value is open) are value bytes, not comments.
  * Duplicate keys are allowed. Writing policy is configurable:
    "all" (default, rewrite every occurrence), "first", "last" or "error".
    Reading uses last-occurrence-wins semantics.
  * Lines starting with '#'/';' are comments even if they look like keys;
    commented-out keys are never matched or modified.

Writing constraints (a violation raises ValueError before any file is
opened, so no half-written result is possible):

  * replacement values must be single-line, must not have leading/trailing
    whitespace, must not end with '\\' (that would open a continuation) and
    must not contain " #" / " ;" (that would be read back as a comment).

The whole file is parsed and validated first. Only if parsing succeeds and
the target key exists is anything written. The write is atomic:

  1. a uniquely-named temp file is created in the SAME directory,
  2. bytes are written, flushed and fsync()ed,
  3. its mode is copied from the original file,
  4. os.replace() makes the new file the target (atomic on POSIX/Windows),
  5. the directory is fsync()ed (best effort).

A crash/kill at any point therefore leaves the target as either the old
bytes or the new bytes, never a truncated mix.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile

__all__ = [
    "ConfigError",
    "KeyNotFound",
    "DuplicateKeyError",
    "parse_text",
    "get_value_text",
    "set_value_text",
    "get_value",
    "set_value",
    "byte_diff",
]

_COMMENT_CHARS = "#;"
_WS = " \t"
_SECTION_RE = re.compile(r"\[([^\]\r\n]*)\][ \t]*(?:[#;].*)?$")
_MISSING = object()


class ConfigError(ValueError):
    """Syntax/structure error with a 1-based line number."""

    def __init__(self, line, reason):
        self.line = line
        self.reason = reason
        super().__init__("line %d: %s" % (line, reason))


class KeyNotFound(KeyError):
    """The requested key/section does not exist in the config."""


class DuplicateKeyError(ValueError):
    """on_duplicate='error' and the key occurs more than once."""


class _Entry:
    __slots__ = ("section", "key", "first", "last", "vstart")

    def __init__(self, section, key, first, last, vstart):
        self.section = section
        self.key = key
        self.first = first
        self.last = last
        self.vstart = vstart


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _split_physical_lines(text):
    """Split on '\\n' only (preserving endings). Returns [body, ending] pairs.

    A trailing newline does NOT produce a phantom final empty line.
    """
    parts = text.split("\n")
    trailing_newline = len(parts) > 1 and parts[-1] == ""
    if trailing_newline:
        parts.pop()
    lines = []
    last_index = len(parts) - 1
    for i, part in enumerate(parts):
        ending = "\n" if (i < last_index or trailing_newline) else ""
        body = part
        if body.endswith("\r"):
            body = body[:-1]
            ending = "\r" + ending
        lines.append([body, ending])
    return lines


def _value_core_end(body, vstart):
    """Index just past the meaningful value chars on one physical line.

    Respects an inline comment (' #'/' ;') and trailing spaces/tabs.
    """
    n = len(body)
    region_end = n
    i = vstart
    while i < n:
        if body[i] in _COMMENT_CHARS and i > vstart and body[i - 1] in _WS:
            region_end = i
            break
        i += 1
    core = body[vstart:region_end].rstrip(_WS)
    return vstart + len(core)


def _parse(text):
    """Return (lines, entries). Raises ConfigError on bad structure."""
    lines = _split_physical_lines(text)
    entries = []
    section = None
    i = 0
    n = len(lines)
    while i < n:
        body = lines[i][0]
        lineno = i + 1
        stripped = body.strip(_WS)

        if not stripped:
            i += 1
            continue
        first_char = stripped[0]
        if first_char in _COMMENT_CHARS:
            i += 1
            continue

        if first_char == "[":
            match = _SECTION_RE.fullmatch(body.lstrip(_WS))
            if match is None:
                raise ConfigError(
                    lineno,
                    "malformed section header (expected '[name]', "
                    "optionally followed by a comment)",
                )
            name = match.group(1).strip(_WS)
            if not name:
                raise ConfigError(lineno, "empty section name")
            section = name
            i += 1
            continue

        eq = body.find("=")
        if eq < 0:
            raise ConfigError(
                lineno, "not a valid entry (expected 'key = value')"
            )
        key = body[:eq].strip(_WS)
        if not key:
            raise ConfigError(lineno, "empty key (text before '=')")

        vstart = eq + 1
        while vstart < len(body) and body[vstart] in _WS:
            vstart += 1

        last = i
        while lines[last][0].endswith("\\") and last + 1 < n:
            last += 1

        entries.append(_Entry(section, key, i, last, vstart))
        i = last + 1

    return lines, entries


def _entry_value(lines, entry):
    first_body = lines[entry.first][0]
    if entry.first == entry.last:
        end = _value_core_end(first_body, entry.vstart)
        return first_body[entry.vstart:end]

    parts = []
    chunk = first_body[entry.vstart:]
    if chunk.endswith("\\"):
        chunk = chunk[:-1]
    parts.append(chunk.rstrip(_WS))
    for idx in range(entry.first + 1, entry.last + 1):
        body = lines[idx][0]
        if idx == entry.last:
            start = len(body) - len(body.lstrip(_WS))
            end = _value_core_end(body, start)
            parts.append(body[start:end])
        else:
            chunk = body.lstrip(_WS)
            if chunk.endswith("\\"):
                chunk = chunk[:-1]
            parts.append(chunk.rstrip(_WS))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _validate_key(key):
    if not isinstance(key, str) or not key:
        raise ValueError("key must be a non-empty string")
    if key != key.strip(_WS):
        raise ValueError("key must not have leading/trailing whitespace")
    if "=" in key or "\n" in key or "\r" in key:
        raise ValueError("key must not contain '=' or line breaks")
    if key[0] in _COMMENT_CHARS + "[":
        raise ValueError("key must not start with '#', ';' or '['")


def _validate_section(section):
    if section is None:
        return
    if not isinstance(section, str) or not section:
        raise ValueError("section must be a non-empty string or None")
    if section != section.strip(_WS):
        raise ValueError("section must not have leading/trailing whitespace")
    if "[" in section or "]" in section or "\n" in section or "\r" in section:
        raise ValueError("section must not contain '[', ']' or line breaks")


def _validate_value(value):
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    if "\n" in value or "\r" in value:
        raise ValueError(
            "replacement value must be a single line (no \\n / \\r)"
        )
    if value != value.strip(_WS):
        raise ValueError("replacement value must not have leading/trailing whitespace")
    if value.endswith("\\"):
        raise ValueError(
            "replacement value must not end with '\\' (line-continuation marker)"
        )
    if re.search(r"[ \t][#;]", value) is not None:
        raise ValueError(
            "replacement value must not contain ' #' or ' ;' "
            "(it would be read back as an inline comment)"
        )


# ---------------------------------------------------------------------------
# in-memory API (operates on decoded text; round trips are byte-exact)
# ---------------------------------------------------------------------------

def parse_text(text):
    """Validate/parse text. Returns a list of (section, key, value) tuples."""
    lines, entries = _parse(text)
    return [(e.section, e.key, _entry_value(lines, e)) for e in entries]


def get_value_text(text, key, *, section=None, default=_MISSING):
    """Read one value; duplicate keys resolve to the last occurrence."""
    _validate_key(key)
    _validate_section(section)
    lines, entries = _parse(text)
    result = default
    found = False
    for entry in entries:
        if entry.key == key and entry.section == section:
            result = _entry_value(lines, entry)
            found = True
    if not found and default is _MISSING:
        where = ("section %r" % section) if section is not None else "top level"
        raise KeyNotFound("key %r not found (%s)" % (key, where))
    return result


def _replacement(lines, entry, new_value):
    first_body, first_ending = lines[entry.first]
    if entry.first == entry.last:
        end = _value_core_end(first_body, entry.vstart)
        suffix = first_body[end:]
    else:
        # multi-line value collapses onto the first line; the trailing
        # whitespace/comment of the LAST physical line is carried over.
        last_body = lines[entry.last][0]
        start = len(last_body) - len(last_body.lstrip(_WS))
        end = _value_core_end(last_body, start)
        suffix = last_body[end:]
    return first_body[: entry.vstart] + new_value + suffix + first_ending


def set_value_text(text, key, value, *, section=None, on_duplicate="all"):
    """Return (new_text, changed). Never mutates the input."""
    _validate_key(key)
    _validate_section(section)
    _validate_value(value)
    if on_duplicate not in ("all", "first", "last", "error"):
        raise ValueError(
            "on_duplicate must be one of: all, first, last, error"
        )

    lines, entries = _parse(text)
    matches = [e for e in entries if e.key == key and e.section == section]
    if not matches:
        where = ("section %r" % section) if section is not None else "top level"
        raise KeyNotFound("key %r not found (%s)" % (key, where))
    if on_duplicate == "error" and len(matches) > 1:
        raise DuplicateKeyError(
            "key %r occurs %d times" % (key, len(matches))
        )
    if on_duplicate == "first":
        targets = matches[:1]
    elif on_duplicate == "last":
        targets = matches[-1:]
    else:
        targets = matches

    pieces = []
    cursor = 0
    for entry in targets:
        for idx in range(cursor, entry.first):
            body, ending = lines[idx]
            pieces.append(body + ending)
        pieces.append(_replacement(lines, entry, value))
        cursor = entry.last + 1
    for idx in range(cursor, len(lines)):
        body, ending = lines[idx]
        pieces.append(body + ending)

    new_text = "".join(pieces)
    return new_text, new_text != text


# ---------------------------------------------------------------------------
# file API
# ---------------------------------------------------------------------------

def _decode(data):
    return data.decode("utf-8", "surrogateescape")


def _encode(text):
    return text.encode("utf-8", "surrogateescape")


def get_value(path, key, *, section=None, default=_MISSING):
    with open(path, "rb") as handle:
        data = handle.read()
    return get_value_text(_decode(data), key, section=section, default=default)


def _atomic_write(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    mode = stat.S_IMODE(os.stat(path).st_mode)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".%s." % os.path.basename(path), suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            # replace never happened (or failed): remove the temp file.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    _fsync_directory(directory)


def _fsync_directory(directory):
    dir_fd = None
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        os.fsync(dir_fd)
    except OSError:
        # Some platforms/filesystems do not support fsync on a directory.
        pass
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def set_value(path, key, value, *, section=None, on_duplicate="all"):
    """Rewrite one key in place. Returns True if bytes were changed.

    Parsing/validation happens before any write. If the new value equals the
    old value the file is not touched at all (idempotent).
    """
    with open(path, "rb") as handle:
        data = handle.read()
    new_text, changed = set_value_text(
        _decode(data), key, value, section=section, on_duplicate=on_duplicate
    )
    if not changed:
        return False
    _atomic_write(os.fspath(path), _encode(new_text))
    return True


# ---------------------------------------------------------------------------
# byte-level diff helper (used by the self-tests and reports)
# ---------------------------------------------------------------------------

def byte_diff(old, new):
    """Return (regions, equal_bytes) where regions is a list of dicts.

    regions: [{op, old_start, old_end, new_start, new_end,
               old_bytes, new_bytes}] using difflib opcodes.
    """
    import difflib

    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    regions = []
    equal_bytes = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            equal_bytes += i2 - i1
        else:
            regions.append(
                {
                    "op": tag,
                    "old_start": i1,
                    "old_end": i2,
                    "new_start": j1,
                    "new_end": j2,
                    "old_bytes": old[i1:i2],
                    "new_bytes": new[j1:j2],
                }
            )
    return regions, equal_bytes

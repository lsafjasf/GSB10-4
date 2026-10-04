"""Byte-preserving in-place rewriter for INI-style config files (stdlib only).

Public API: rewrite_value(path, key, value, section=None) -> dict

File format (strict, documented):
- Line kinds: blank, comment (first non-blank char is '#' or ';'),
  section header "[name]", and "key = value" / "key : value".
- Keys are case-sensitive; the file's original casing is never altered.
- Inline comment: a value may be followed by whitespace + '#' / ';'
  (or the value itself may start with '#' / ';'). The comment is preserved.
- Continuation: a non-blank, non-comment line indented deeper than the
  preceding key line extends that key's value. Replacing such a key
  replaces the whole multi-line value and removes its continuation lines.
- Duplicate keys inside the same section are rejected (DuplicateKeyError).
- Keys may appear before any section (addressed with section=None).
- Anything else is a syntax error reported with its 1-based line number.

Write semantics:
- Only the bytes of the target value (and its continuation lines) change;
  every other byte of the file is preserved exactly.
- If the stored value already equals the requested value, nothing is
  written at all (idempotent no-op).
- The file is fully parsed and validated BEFORE anything is written; a
  rejected input never produces a partial write.
- Writes are atomic: temp file in the same directory + fsync + os.replace
  + directory fsync. A crash either leaves the old file or the new file,
  never a truncated one. The original file mode is preserved.
- Read-only files are refused up front (ReadOnlyFileError).
"""

from __future__ import annotations

import os
import re
import signal
import stat
import tempfile

__all__ = [
    "ConfigError",
    "ConfigSyntaxError",
    "DuplicateKeyError",
    "KeyNotFoundError",
    "ReadOnlyFileError",
    "rewrite_value",
]


class ConfigError(Exception):
    """Base class for all config_rewrite errors."""


class ConfigSyntaxError(ConfigError):
    def __init__(self, line_no, reason):
        self.line_no = line_no
        self.reason = reason
        super().__init__("line %d: %s" % (line_no, reason))


class DuplicateKeyError(ConfigError):
    def __init__(self, section, key, first_line, line_no):
        self.section = section
        self.key = key
        self.first_line = first_line
        self.line_no = line_no
        where = "section [%s]" % section if section is not None else "top level"
        super().__init__(
            "line %d: duplicate key %r in %s (first defined on line %d)"
            % (line_no, key, where, first_line)
        )


class KeyNotFoundError(ConfigError):
    def __init__(self, section, key):
        where = "section [%s]" % section if section is not None else "top level"
        super().__init__("key %r not found in %s" % (key, where))


class ReadOnlyFileError(ConfigError):
    def __init__(self, path):
        super().__init__("refusing to modify read-only file: %s" % path)


_SECTION_RE = re.compile(rb"^[ \t]*\[([^\]\r\n]*)\][ \t]*(?:[#;][^\r\n]*)?$")
_KEY_RE = re.compile(
    rb"^([ \t]*)([^ \t=\[:#;][^=:\r\n]*?)([ \t]*)([=:])([ \t]*)([^\r\n]*)$"
)
_INLINE_COMMENT_RE = re.compile(rb"[ \t][#;]")


def _parse(content, want_key, want_section):
    """Streaming parse: validate every line and locate the one target key.

    Returns the target's byte offsets/details; nothing per-line is retained,
    so memory stays close to the file size plus the duplicate-key set.
    """
    n = len(content)
    pos = 0
    line_no = 0
    section = b""
    seen = {}
    last_indent = -1
    found = None
    last_target = None
    while pos < n:
        line_no += 1
        nl = content.find(b"\n", pos)
        line_start = pos
        next_pos = n + 1 if nl == -1 else nl + 1
        body_end = n if nl == -1 else nl
        if body_end > pos and content[body_end - 1:body_end] == b"\r":
            body_end -= 1
        body = content[pos:body_end]
        pos = next_pos

        stripped = body.strip()
        if stripped == b"":
            continue
        if stripped[:1] in (b"#", b";"):
            continue
        m = _SECTION_RE.match(body)
        if m:
            name = m.group(1).strip()
            if not name:
                raise ConfigSyntaxError(line_no, "empty section name")
            section = name
            last_indent = -1
            continue
        indent = len(body) - len(body.lstrip(b" \t"))
        km = _KEY_RE.match(body)
        if last_indent >= 0 and indent > last_indent:
            # deeper-indented line: continuation of the previous key's value
            if last_target is not None:
                last_target["end_next"] = next_pos
                last_target["continuation"] += 1
            continue  # baseline indent stays the key line's indent
        if km is None:
            raise ConfigSyntaxError(
                line_no, "unparseable line: %r" % (body[:60],)
            )
        key_raw = km.group(2)
        last_indent = indent
        sig = section + b"\x00" + key_raw
        if sig in seen:
            raise DuplicateKeyError(
                section, key_raw, seen[sig], line_no)
        seen[sig] = line_no

        value_start = line_start + km.start(6)
        value_part = content[value_start:body_end]
        if value_part[:1] in (b"#", b";"):
            comment_abs = value_start
        else:
            cm = _INLINE_COMMENT_RE.search(value_part)
            if cm is None:
                comment_abs = None
            else:
                cp = value_start + cm.start() + 1  # absolute index of # / ;
                value_trimmed = content[value_start:cp].rstrip(b" \t")
                comment_abs = value_start + len(value_trimmed)
        value_end = comment_abs if comment_abs is not None else body_end
        if key_raw == want_key and section == want_section and found is None:
            found = {
                "line_no": line_no,
                "value_start": value_start,
                "value_end": value_end,
                "line_next": next_pos,
                "end_next": next_pos,
                "continuation": 0,
            }
            last_target = found
        else:
            last_target = None
    return found


def _atomic_write(path, data, mode):
    directory = os.path.dirname(os.path.abspath(path))
    base = os.path.basename(path)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % base, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        # Test-only power-loss hook: die after the temp file is durable
        # but before the rename, exactly like a crash at the worst point.
        if os.environ.get("CONFIG_REWRITE_CRASH_BEFORE_RENAME"):
            os.kill(os.getpid(), signal.SIGKILL)
        os.replace(tmp, path)
        dirfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def rewrite_value(path, key, value, section=None):
    """Replace the value of `key` (in `section`, None = top level) in `path`.

    Returns a report dict. Raises ConfigError subclasses on any problem;
    on error the file is guaranteed to be untouched.
    """
    key_b = key.encode("utf-8") if isinstance(key, str) else bytes(key)
    value_b = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    section_b = (
        section.encode("utf-8") if isinstance(section, str)
        else (bytes(section) if section is not None else None)
    )
    if b"\n" in value_b or b"\r" in value_b:
        raise ConfigError("new value must be a single line")

    st = os.stat(path)  # FileNotFoundError propagates
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o222 == 0 or not os.access(path, os.W_OK):
        raise ReadOnlyFileError(path)

    with open(path, "rb") as f:
        content = f.read()

    target = _parse(content, key_b, section_b if section_b is not None else b"")
    if target is None:
        raise KeyNotFoundError(section, key)

    old_value = content[target["value_start"]:target["value_end"]]
    if old_value == value_b:
        return {
            "changed": False,
            "path": path,
            "key": key,
            "section": section,
            "reason": "value already up to date; nothing written",
        }

    # splice: keep every byte outside the value span; the value span covers
    # the value itself plus any continuation lines of the old value.
    new_content = (
        content[: target["value_start"]]
        + value_b
        + content[target["value_end"]:target["line_next"]]
        + content[target["end_next"]:]
    )

    _atomic_write(path, new_content, mode)
    return {
        "changed": True,
        "path": path,
        "key": key,
        "section": section,
        "line": target["line_no"],
        "old_value": old_value,
        "new_value": value_b,
        "continuation_lines_removed": target["continuation"],
        "bytes_before": len(content),
        "bytes_after": len(new_content),
    }


def main(argv):
    import sys

    if len(argv) != 5:
        sys.stderr.write(
            "usage: python3 config_rewrite.py FILE SECTION KEY VALUE\n"
            "       (use '-' as SECTION for top-level keys)\n"
        )
        return 2
    _, path, section, key, value = argv
    section = None if section == "-" else section
    try:
        report = rewrite_value(path, key, value, section=section)
    except ConfigError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    for k, v in report.items():
        print("%s: %s" % (k, v))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv))

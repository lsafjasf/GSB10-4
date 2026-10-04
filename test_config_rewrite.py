"""Tests for config_rewrite: byte-level diffs, idempotency, atomicity, edges."""

import os
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_rewrite as cr


def diff_offsets(a, b):
    """Return the list of byte offsets at which a and b differ."""
    n = min(len(a), len(b))
    offs = [i for i in range(n) if a[i] != b[i]]
    offs.extend(range(n, max(len(a), len(b))))
    return offs


def diff_report(old, new):
    """Human-readable byte-level diff report."""
    offs = diff_offsets(old, new)
    lines = [
        "bytes: %d -> %d (delta %+d), differing offsets: %d"
        % (len(old), len(new), len(new) - len(old), len(offs))
    ]
    for off in offs[:10]:
        lo = max(0, off - 12)
        lines.append(
            "  @%d old=%r new=%r"
            % (off, old[lo:off + 12], new[lo:off + 12])
        )
    return "\n".join(lines)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, content, name="app.ini", mode=None):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(content)
        if mode is not None:
            os.chmod(path, mode)
        return path

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def stray_tmps(self):
        return [n for n in os.listdir(self.dir) if n.endswith(".tmp")]


class TestBytePreservation(Base):
    def test_no_comments_basic(self):
        before = b"a = 1\nb = 2\nc = 3\n"
        path = self.make(before)
        rep = cr.rewrite_value(path, "b", "9")
        after = self.read(path)
        self.assertTrue(rep["changed"])
        self.assertEqual(after, b"a = 1\nb = 9\nc = 3\n")
        # equal-length replace: exactly one differing byte, at the value
        offs = diff_offsets(before, after)
        self.assertEqual(offs, [10])
        print("\n[byte-diff] basic replace:\n" + diff_report(before, after))

    def test_length_change_shifts_only_tail(self):
        before = b"a = 1\nb = 2\nc = 3\n"
        path = self.make(before)
        cr.rewrite_value(path, "b", "20")
        after = self.read(path)
        self.assertEqual(after, b"a = 1\nb = 20\nc = 3\n")
        # every byte before the value region is untouched
        self.assertEqual(before[:8], after[:8])
        print("\n[byte-diff] length change:\n" + diff_report(before, after))

    def test_trailing_comment_preserved(self):
        before = b"threads = 4   # worker count\n"
        path = self.make(before)
        cr.rewrite_value(path, "threads", "8")
        after = self.read(path)
        self.assertEqual(after, b"threads = 8   # worker count\n")
        # comment bytes identical
        self.assertEqual(before[before.index(b"#"):], after[after.index(b"#"):])
        print("\n[byte-diff] trailing comment:\n" + diff_report(before, after))

    def test_comment_only_value(self):
        before = b"opt = # disabled for now\n"
        path = self.make(before)
        cr.rewrite_value(path, "opt", "on")
        self.assertEqual(self.read(path), b"opt = on# disabled for now\n")

    def test_continuation_value_replaced(self):
        before = (
            b"[app]\n"
            b"path = /one\n"
            b"    /two\n"
            b"\t/three\n"
            b"other = x\n"
        )
        path = self.make(before)
        rep = cr.rewrite_value(path, "path", "/new", section="app")
        after = self.read(path)
        self.assertEqual(after, b"[app]\npath = /new\nother = x\n")
        self.assertEqual(rep["continuation_lines_removed"], 2)
        print("\n[byte-diff] continuation:\n" + diff_report(before, after))

    def test_commented_out_key_untouched(self):
        before = b"# secret = old\n; secret = older\nsecret = real\n"
        path = self.make(before)
        cr.rewrite_value(path, "secret", "new")
        after = self.read(path)
        self.assertEqual(after, b"# secret = old\n; secret = older\nsecret = new\n")

    def test_key_case_preserved_and_case_sensitive(self):
        before = b"ServerName = alpha\nservername = lower\n"
        path = self.make(before)
        cr.rewrite_value(path, "ServerName", "beta")
        after = self.read(path)
        self.assertEqual(after, b"ServerName = beta\nservername = lower\n")
        with self.assertRaises(cr.KeyNotFoundError):
            cr.rewrite_value(path, "SERVERNAME", "x")

    def test_crlf_preserved(self):
        before = b"a = 1\r\nb = 2\r\n"
        path = self.make(before)
        cr.rewrite_value(path, "a", "9")
        after = self.read(path)
        self.assertEqual(after, b"a = 9\r\nb = 2\r\n")

    def test_no_trailing_newline(self):
        before = b"x = 1\nz = 9"
        path = self.make(before)
        cr.rewrite_value(path, "z", "10")
        self.assertEqual(self.read(path), b"x = 1\nz = 10")

    def test_indentation_and_separator_style_kept(self):
        before = b"  \tkey\t=\told value\n"
        path = self.make(before)
        cr.rewrite_value(path, "key", "new")
        self.assertEqual(self.read(path), b"  \tkey\t=\tnew\n")

    def test_section_targeting(self):
        before = b"[a]\nport = 1\n[b]\nport = 2\n"
        path = self.make(before)
        cr.rewrite_value(path, "port", "99", section="b")
        self.assertEqual(self.read(path), b"[a]\nport = 1\n[b]\nport = 99\n")

    def test_hash_inside_value_without_space_is_literal(self):
        before = b"opt = a#b\n"
        path = self.make(before)
        cr.rewrite_value(path, "opt", "c#d")
        self.assertEqual(self.read(path), b"opt = c#d\n")


class TestIdempotency(Base):
    def test_second_run_is_byte_identical_noop(self):
        before = b"[net]\nhost = old.example  # primary\nport = 80\n"
        path = self.make(before)
        rep1 = cr.rewrite_value(path, "host", "new.example", section="net")
        after1 = self.read(path)
        rep2 = cr.rewrite_value(path, "host", "new.example", section="net")
        after2 = self.read(path)
        self.assertTrue(rep1["changed"])
        self.assertFalse(rep2["changed"])
        self.assertEqual(after1, after2)
        self.assertEqual(diff_offsets(after1, after2), [])
        print("\n[idempotency] 2nd run diff: %d differing bytes, changed=%s"
              % (len(diff_offsets(after1, after2)), rep2["changed"]))

    def test_idempotent_after_continuation_removal(self):
        before = b"k = a\n    b\n    c\n"
        path = self.make(before)
        cr.rewrite_value(path, "k", "z")
        after1 = self.read(path)
        rep2 = cr.rewrite_value(path, "k", "z")
        self.assertFalse(rep2["changed"])
        self.assertEqual(self.read(path), after1)


class TestRejection(Base):
    def test_syntax_error_reports_line_and_writes_nothing(self):
        before = b"good = 1\n= orphan value\nalso = fine\n"
        path = self.make(before)
        with self.assertRaises(cr.ConfigSyntaxError) as ctx:
            cr.rewrite_value(path, "also", "changed")
        self.assertEqual(ctx.exception.line_no, 2)
        self.assertEqual(self.read(path), before)
        self.assertEqual(self.stray_tmps(), [])
        print("\n[reject] syntax error -> %s" % ctx.exception)

    def test_garbage_line_rejected(self):
        path = self.make(b"a = 1\n!!! not a config line !!!\n")
        with self.assertRaises(cr.ConfigSyntaxError) as ctx:
            cr.rewrite_value(path, "a", "2")
        self.assertEqual(ctx.exception.line_no, 2)

    def test_empty_section_name_rejected(self):
        path = self.make(b"[]\nk = 1\n")
        with self.assertRaises(cr.ConfigSyntaxError) as ctx:
            cr.rewrite_value(path, "k", "2")
        self.assertEqual(ctx.exception.line_no, 1)

    def test_duplicate_keys_rejected_with_lines(self):
        before = b"[s]\nk = 1\nother = 0\nk = 2\n"
        path = self.make(before)
        with self.assertRaises(cr.DuplicateKeyError) as ctx:
            cr.rewrite_value(path, "other", "9", section="s")
        self.assertEqual(ctx.exception.line_no, 4)
        self.assertEqual(ctx.exception.first_line, 2)
        self.assertEqual(self.read(path), before)
        print("\n[reject] duplicate -> %s" % ctx.exception)

    def test_same_key_in_different_sections_is_ok(self):
        path = self.make(b"[a]\nk = 1\n[b]\nk = 2\n")
        rep = cr.rewrite_value(path, "k", "3", section="b")
        self.assertTrue(rep["changed"])

    def test_key_not_found(self):
        path = self.make(b"a = 1\n")
        with self.assertRaises(cr.KeyNotFoundError):
            cr.rewrite_value(path, "missing", "x")

    def test_multiline_value_rejected(self):
        path = self.make(b"a = 1\n")
        with self.assertRaises(cr.ConfigError):
            cr.rewrite_value(path, "a", "x\ny")

    def test_readonly_file_refused_and_untouched(self):
        before = b"a = 1\n"
        path = self.make(before, mode=0o444)
        try:
            with self.assertRaises(cr.ReadOnlyFileError):
                cr.rewrite_value(path, "a", "2")
            self.assertEqual(self.read(path), before)
            self.assertEqual(self.stray_tmps(), [])
            print("\n[reject] read-only file refused, bytes unchanged")
        finally:
            os.chmod(path, 0o644)

    def test_file_mode_preserved_on_write(self):
        path = self.make(b"a = 1\n", mode=0o640)
        cr.rewrite_value(path, "a", "2")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)


class TestAtomicity(Base):
    def test_failed_rename_leaves_original_and_no_temp(self):
        before = b"a = 1\n"
        path = self.make(before)
        real_replace = os.replace
        def boom(src, dst):
            raise OSError("simulated rename failure")
        os.replace = boom
        try:
            with self.assertRaises(OSError):
                cr.rewrite_value(path, "a", "2")
        finally:
            os.replace = real_replace
        self.assertEqual(self.read(path), before)
        self.assertEqual(self.stray_tmps(), [])
        print("\n[atomicity] rename failure: original intact, no temp left")

    def test_power_loss_sigkill_before_rename(self):
        before = b"[db]\nhost = old\n" + b"".join(
            b"filler%05d = x\n" % i for i in range(2000))
        path = self.make(before)
        env = dict(os.environ, CONFIG_REWRITE_CRASH_BEFORE_RENAME="1")
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r);"
             "import config_rewrite as cr;"
             "cr.rewrite_value(%r, 'host', 'new', section='db')"
             % (os.path.dirname(os.path.abspath(__file__)), path)],
            env=env, capture_output=True,
        )
        self.assertEqual(proc.returncode, -9, proc.stderr)
        # original file must be byte-identical: all-or-nothing
        self.assertEqual(self.read(path), before)
        # the orphaned temp (if any) holds the COMPLETE new content,
        # never a truncated target file
        tmps = self.stray_tmps()
        for name in tmps:
            data = self.read(os.path.join(self.dir, name))
            self.assertIn(b"host = new", data)
            self.assertTrue(data.endswith(b"filler01999 = x\n"))
        print("\n[atomicity] SIGKILL before rename: exit=%s, original intact, "
              "%d complete temp file(s) orphaned" % (proc.returncode, len(tmps)))
        # a subsequent normal run still works
        rep = cr.rewrite_value(path, "host", "new", section="db")
        self.assertTrue(rep["changed"])
        self.assertIn(b"host = new\n", self.read(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)

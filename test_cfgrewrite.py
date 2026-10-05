"""Self-tests for cfgrewrite. Run: python3 -m unittest test_cfgrewrite -v"""

import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cfgrewrite
from cfgrewrite import (
    ConfigError,
    DuplicateKeyError,
    KeyNotFound,
    byte_diff,
    get_value,
    get_value_text,
    set_value,
    set_value_text,
)

HERE = os.path.dirname(os.path.abspath(__file__))


class TempDirMixin(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cfgrewrite-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def path(self, name="app.conf"):
        return os.path.join(self.dir, name)

    def write(self, data, name="app.conf", mode=0o644):
        p = self.path(name)
        with open(p, "wb") as fh:
            fh.write(data)
        os.chmod(p, mode)
        return p

    def read(self, name="app.conf"):
        with open(self.path(name), "rb") as fh:
            return fh.read()

    def leftover_temps(self):
        return [f for f in os.listdir(self.dir) if f.endswith(".tmp")]


class ReadTests(unittest.TestCase):
    def test_read_simple(self):
        text = "host = localhost\nport = 8080\n"
        self.assertEqual(get_value_text(text, "host"), "localhost")
        self.assertEqual(get_value_text(text, "port"), "8080")

    def test_read_strips_inline_comment(self):
        text = "host = localhost   # primary\n"
        self.assertEqual(get_value_text(text, "host"), "localhost")

    def test_read_continuation(self):
        text = "paths = /a \\\n        /b \\\n        /c  # tail\n"
        self.assertEqual(get_value_text(text, "paths"), "/a\n/b\n/c")

    def test_read_duplicate_last_wins(self):
        text = "k = 1\nk = 2\n"
        self.assertEqual(get_value_text(text, "k"), "2")

    def test_read_commented_key_ignored(self):
        text = "# k = old\n; k = older\nk = new\n"
        self.assertEqual(get_value_text(text, "k"), "new")

    def test_read_missing(self):
        with self.assertRaises(KeyNotFound):
            get_value_text("a = 1\n", "nope")
        self.assertIsNone(get_value_text("a = 1\n", "nope", default=None))

    def test_read_section(self):
        text = "[db]\nhost = x\n[web]\nhost = y\n"
        self.assertEqual(get_value_text(text, "host", section="db"), "x")
        self.assertEqual(get_value_text(text, "host", section="web"), "y")
        with self.assertRaises(KeyNotFound):
            get_value_text(text, "host")


class SyntaxErrorTests(unittest.TestCase):
    def _assert_line(self, text, line):
        with self.assertRaises(ConfigError) as ctx:
            get_value_text(text, "k", default=None)
        self.assertEqual(ctx.exception.line, line, str(ctx.exception))
        self.assertIn("line %d" % line, str(ctx.exception))

    def test_line_without_equals(self):
        self._assert_line("k = 1\njust some words\n", 2)

    def test_empty_key(self):
        self._assert_line("k = 1\n= v\n", 2)

    def test_unterminated_section(self):
        self._assert_line("k = 1\n[broken\n", 2)

    def test_junk_after_section(self):
        self._assert_line("[ok]\nk = 1\n[sec] trailing-junk\n", 3)

    def test_empty_section_name(self):
        self._assert_line("[]\n", 1)

    def test_error_reports_reason(self):
        try:
            get_value_text("hello world\n", "k", default=None)
        except ConfigError as exc:
            self.assertEqual(exc.line, 1)
            self.assertIn("key = value", exc.reason)
        else:
            self.fail("expected ConfigError")


class WriteTests(TempDirMixin):
    def test_no_comments_only_value_bytes_change(self):
        p = self.write(b"host = localhost\nport = 8080\n")
        before = self.read()
        self.assertTrue(set_value(p, "host", "example.com"))
        after = self.read()
        self.assertEqual(after, b"host = example.com\nport = 8080\n")
        # every byte outside the value span is identical
        vstart = before.index(b"localhost")
        vend = vstart + len(b"localhost")
        self.assertEqual(after[:vstart], before[:vstart])
        self.assertEqual(after[vstart + len(b"example.com"):], before[vend:])
        regions, _ = byte_diff(before, after)
        self.assertTrue(all(r["old_start"] >= vstart and r["old_end"] <= vend
                            for r in regions))

    def test_trailing_comment_and_spacing_preserved(self):
        p = self.write(b"host   =   localhost   # primary host\n")
        set_value(p, "host", "example.com")
        self.assertEqual(self.read(), b"host   =   example.com   # primary host\n")

    def test_commented_out_keys_untouched(self):
        p = self.write(b"# host = old\n;host = older\nhost = new\n")
        set_value(p, "host", "changed")
        self.assertEqual(self.read(), b"# host = old\n;host = older\nhost = changed\n")

    def test_key_case_preserved_and_case_sensitive(self):
        p = self.write(b"Host = a\nHOST = b\nhost = c\n")
        set_value(p, "host", "z")
        self.assertEqual(self.read(), b"Host = a\nHOST = b\nhost = z\n")

    def test_continuation_value_collapsed_comment_kept(self):
        p = self.write(b"paths = /a \\\n        /b \\\n        /c  # search path\nnext = 1\n")
        set_value(p, "paths", "/single")
        self.assertEqual(self.read(), b"paths = /single  # search path\nnext = 1\n")

    def test_duplicate_all_first_last_error(self):
        original = b"k = 1\nx = 9\nk = 2\n"
        p = self.write(original)
        set_value(p, "k", "0")  # default: all
        self.assertEqual(self.read(), b"k = 0\nx = 9\nk = 0\n")

        p = self.write(original)
        set_value(p, "k", "0", on_duplicate="first")
        self.assertEqual(self.read(), b"k = 0\nx = 9\nk = 2\n")

        p = self.write(original)
        set_value(p, "k", "0", on_duplicate="last")
        self.assertEqual(self.read(), b"k = 1\nx = 9\nk = 0\n")

        p = self.write(original)
        with self.assertRaises(DuplicateKeyError):
            set_value(p, "k", "0", on_duplicate="error")
        self.assertEqual(self.read(), original)  # untouched

    def test_section_targeting(self):
        p = self.write(b"host = top\n[db]\nhost = dbhost\n")
        set_value(p, "host", "new", section="db")
        self.assertEqual(self.read(), b"host = top\n[db]\nhost = new\n")

    def test_crlf_preserved(self):
        p = self.write(b"a = 1\r\nb = 2\r\n")
        set_value(p, "a", "10")
        self.assertEqual(self.read(), b"a = 10\r\nb = 2\r\n")

    def test_no_trailing_newline(self):
        p = self.write(b"a = 1\nb = 2")
        set_value(p, "b", "3")
        self.assertEqual(self.read(), b"a = 1\nb = 3")

    def test_trailing_whitespace_after_value_preserved(self):
        p = self.write(b"a = 1   \n")
        set_value(p, "a", "22")
        self.assertEqual(self.read(), b"a = 22   \n")

    def test_key_not_found_no_write(self):
        p = self.write(b"a = 1\n")
        with self.assertRaises(KeyNotFound):
            set_value(p, "missing", "x")
        self.assertEqual(self.read(), b"a = 1\n")

    def test_syntax_error_no_partial_write(self):
        original = b"a = 1\nbroken line here\n"
        p = self.write(original)
        with self.assertRaises(ConfigError) as ctx:
            set_value(p, "a", "2")
        self.assertEqual(ctx.exception.line, 2)
        self.assertEqual(self.read(), original)
        self.assertEqual(self.leftover_temps(), [])

    def test_invalid_value_rejected_before_write(self):
        original = b"a = 1\n"
        p = self.write(original)
        for bad in ("x\ny", "x\ry", " lead", "trail ", "ends\\", "has # comment"):
            with self.assertRaises((ValueError, TypeError), msg=repr(bad)):
                set_value(p, "a", bad)
            self.assertEqual(self.read(), original)
            self.assertEqual(self.leftover_temps(), [])

    def test_value_with_hash_no_space_ok(self):
        p = self.write(b"a = 1\n")
        set_value(p, "a", "C#rock")
        self.assertEqual(get_value(p, "a"), "C#rock")


class IdempotencyTests(TempDirMixin):
    def test_second_run_byte_identical(self):
        original = (
            b"# cfg\n"
            b"host = localhost   # primary\n"
            b"\n"
            b"[db]\n"
            b"paths = /a \\\n"
            b"        /b\n"
            b"k = 1\n"
            b"k = 2\n"
        )
        p = self.write(original)
        self.assertTrue(set_value(p, "host", "example.com"))
        self.assertTrue(set_value(p, "paths", "/one", section="db"))
        self.assertTrue(set_value(p, "k", "9", section="db"))
        first = self.read()

        # second run with the same operations: not a single byte may change
        mtime_before = os.stat(p).st_mtime_ns
        self.assertFalse(set_value(p, "host", "example.com"))
        self.assertFalse(set_value(p, "paths", "/one", section="db"))
        self.assertFalse(set_value(p, "k", "9", section="db"))
        second = self.read()

        self.assertEqual(first, second)
        self.assertEqual(len(first), len(second))
        diff_positions = [i for i in range(len(first)) if first[i] != second[i]]
        self.assertEqual(diff_positions, [])
        self.assertEqual(os.stat(p).st_mtime_ns, mtime_before,
                         "unchanged file must not be rewritten at all")


class AtomicityTests(TempDirMixin):
    def test_readonly_file_still_rewritten_mode_kept(self):
        p = self.write(b"a = 1\n", mode=0o444)
        self.assertTrue(set_value(p, "a", "2"))
        self.assertEqual(self.read(), b"a = 2\n")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o444)

    def test_readonly_directory_fails_cleanly(self):
        p = self.write(b"a = 1\n")
        os.chmod(self.dir, 0o555)
        try:
            with self.assertRaises(PermissionError):
                set_value(p, "a", "2")
        finally:
            os.chmod(self.dir, 0o755)
        self.assertEqual(self.read(), b"a = 1\n")
        self.assertEqual(self.leftover_temps(), [])

    def test_failure_before_replace_leaves_original(self):
        p = self.write(b"a = 1\n")
        original_replace = os.replace

        def boom(src, dst):
            raise OSError("simulated failure before rename")

        cfgrewrite.os.replace = boom
        try:
            with self.assertRaises(OSError):
                set_value(p, "a", "2")
        finally:
            cfgrewrite.os.replace = original_replace
        self.assertEqual(self.read(), b"a = 1\n")
        self.assertEqual(self.leftover_temps(), [])

    def test_sigkill_mid_write_never_truncates(self):
        """Simulate a power cut: SIGKILL the writer between temp-write and rename."""
        p = self.write(b"host = original\n" + b"# padding\n" * 100)
        before = self.read()

        child = (
            "import os, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "import cfgrewrite\n"
            "orig = os.replace\n"
            "def slow(src, dst):\n"
            "    time.sleep(30)  # temp file fully written+fsynced, rename pending\n"
            "    return orig(src, dst)\n"
            "os.replace = slow\n"
            "cfgrewrite.set_value(sys.argv[1], 'host', 'changed')\n" % HERE
        )
        proc = subprocess.Popen([sys.executable, "-c", child, p])
        time.sleep(1.5)  # let it reach the sleep inside slow()
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        self.assertEqual(proc.returncode, -signal.SIGKILL)

        # the target file is either old or new -- never a truncated mix
        after = self.read()
        self.assertIn(after, (before, b"host = changed\n" + b"# padding\n" * 100))
        self.assertEqual(after, before)  # killed before rename: old bytes

        # a stale temp file may remain after SIGKILL (documented), but the
        # target file itself is intact and the library still works.
        for tmp in self.leftover_temps():
            self.assertTrue(tmp.startswith(".app.conf."))
        self.assertTrue(set_value(p, "host", "changed"))
        self.assertEqual(get_value(p, "host"), "changed")


class ValueConstraintTests(unittest.TestCase):
    def test_bad_keys(self):
        for bad in ("", " x", "x ", "a=b", "a\nb", "#c", "[d"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                set_value_text("a = 1\n", bad, "v")

    def test_bad_duplicate_policy(self):
        with self.assertRaises(ValueError):
            set_value_text("a = 1\n", "a", "v", on_duplicate="bogus")


if __name__ == "__main__":
    unittest.main()

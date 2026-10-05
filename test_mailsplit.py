"""Unit tests for mailsplit -- standard-library unittest only."""

import io
import unittest

import mailsplit
from mailsplit import Message, SEP_RE, split, parse_one, CharsetError


def split_bytes(data: bytes, **kw):
    return list(split(io.BytesIO(data), **kw))


SEP1 = b"From alice@example.com Mon Oct  5 09:00:00 2026"
SEP2 = b"From bob@example.com   Mon Oct  5 10:00:00 2026"  # double space -> not valid


class SeparatorRegexTest(unittest.TestCase):
    def test_valid_separator(self):
        self.assertIsNotNone(
            SEP_RE.match(b"From alice@example.com Mon Oct  5 09:00:00 2026 +0800")
        )
        self.assertIsNotNone(
            SEP_RE.match(b"From a.b+tag@sub.example.org Sun Dec 31 23:59:59 2030")
        )

    def test_near_misses_do_not_match(self):
        # wrong weekday, double spaces, bad date padding, trailing garbage
        for line in (
            b"From alice@example.com Mno Oct  5 09:00:00 2026",
            b"From alice@example.com Mon Oct  5 09:00 2026",
            b"From alice@example.com Mon Oct  5 09:00:00 2026 EXTRA",
            b"Subject: From alice@example.com Mon Oct  5 09:00:00 2026",
        ):
            self.assertIsNone(SEP_RE.match(line), line)


class BasicSplitTest(unittest.TestCase):
    def test_single_message_no_separator(self):
        data = b"Subject: one\n\nbody\n"
        msgs = split_bytes(data)
        self.assertEqual(len(msgs), 1)
        self.assertIsNone(msgs[0].separator)
        self.assertEqual(msgs[0].get("Subject"), b" one")
        self.assertEqual(msgs[0].body(), b"body")

    def test_two_messages(self):
        data = (
            SEP1 + b"\nSubject: a\n\nbody a\n"
            + b"From bob@example.com Tue Oct  6 09:00:00 2026\n"
            + b"Subject: b\n\nbody b\n"
        )
        msgs = split_bytes(data)
        self.assertEqual([m.get("Subject") for m in msgs], [b" a", b" b"])
        self.assertEqual([m.body() for m in msgs], [b"body a", b"body b"])
        self.assertTrue(msgs[0].separator.startswith(b"From "))

    def test_empty_body(self):
        data = SEP1 + b"\nSubject: none\n\n"
        msgs = split_bytes(data)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].body(), b"")

    def test_empty_body_between_separators(self):
        data = (
            SEP1 + b"\nSubject: a\n\nbody\n"
            + b"From bob@example.com Tue Oct  6 09:00:00 2026\n"
            + b"Subject: b\n\n"
        )
        msgs = split_bytes(data)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1].body(), b"")

    def test_missing_headers_bare_separator(self):
        data = SEP1 + b"\n\njust body, no headers\n"
        msgs = split_bytes(data)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].headers(), [])
        self.assertEqual(msgs[0].body(), b"just body, no headers")
        self.assertTrue(any("bare separator" in w for w in msgs[0].warnings))

    def test_missing_headers_strict_mode_rejects(self):
        # With allow_bare_separator=False the bare separator cannot be proven
        # and the whole thing stays body of the first message.
        data = b"Subject: first\n\nx\n" + SEP1 + b"\n\nno headers\n"
        msgs = split_bytes(data, allow_bare_separator=False)
        self.assertEqual(len(msgs), 1)
        self.assertIn(SEP1, msgs[0].body())

    def test_no_trailing_newline(self):
        data = SEP1 + b"\nSubject: a\n\nlast line, no newline"
        msgs = split_bytes(data)
        self.assertEqual(msgs[0].body(), b"last line, no newline")


class SeparatorInBodyTest(unittest.TestCase):
    """The whole point of the exercise: identical line inside a body."""

    def test_unescaped_separator_like_line_followed_by_body(self):
        # Exact same text as a real separator, but followed by ordinary body
        # text (not a header block) -> must stay part of the body.
        fake = SEP1
        data = (
            b"Subject: outer\n\n"
            b"log line before\n"
            + fake + b"\n"
            + b"this is not a header, just prose\n"
            + b"log line after\n"
        )
        msgs = split_bytes(data)
        self.assertEqual(len(msgs), 1)
        body = msgs[0].body()
        self.assertIn(fake, body)
        self.assertTrue(body.endswith(b"log line after"))

    def test_escaped_separator_like_line_is_unescaped(self):
        # mboxrd: writer prefixes '>', reader removes exactly one.
        data = (
            SEP1 + b"\nSubject: outer\n\n"
            b">From a@b Mon Oct  5 09:00:00 2026\n"
            b">>From doubly quoted\n"
        )
        msgs = split_bytes(data)
        self.assertEqual(len(msgs), 1)
        body = msgs[0].body().split(b"\n")
        self.assertEqual(body[0], b"From a@b Mon Oct  5 09:00:00 2026")
        self.assertEqual(body[1], b">From doubly quoted")

    def test_real_separator_still_works_after_escaped_body(self):
        data = (
            SEP1 + b"\nSubject: a\n\n"
            b">From fake Mon Oct  5 09:00:00 2026\n"
            b"From bob@example.com Tue Oct  6 09:00:00 2026\n"
            b"Subject: b\n\nreal body\n"
        )
        msgs = split_bytes(data)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].body(), b"From fake Mon Oct  5 09:00:00 2026")
        self.assertEqual(msgs[1].get("Subject"), b" b")

    def test_heuristic_limit_is_documentable(self):
        # Adversarial: an unescaped separator-looking line *immediately*
        # followed by a valid header block is indistinguishable from a real
        # boundary on textual evidence alone.  The writer-side mboxrd escape
        # is what protects this case -- assert that the escape fixes it.
        adversarial = (
            b"Subject: outer\n\n"
            + SEP1 + b"\n"
            b"X-Sneaky: looks like a real header\n\n"
            b"body continues\n"
        )
        naive = split_bytes(adversarial)
        self.assertEqual(len(naive), 2)  # heuristic split (textually ambiguous)

        protected = split_bytes(b"Subject: outer\n\n" + b">" + SEP1 + b"\n"
                                b"X-Sneaky: looks like a real header\n\n"
                                b"body continues\n")
        self.assertEqual(len(protected), 1)
        self.assertIn(SEP1, protected[0].body())

    def test_separator_like_line_inside_first_header_section(self):
        # Separator-looking junk before the first blank line, no headers after
        # -> demoted to body with a warning instead of splitting.
        data = SEP1 + b"\nsome junk without colon\n\nbody\n"
        msgs = split_bytes(data)
        self.assertEqual(len(msgs), 1)
        self.assertIn(b"some junk without colon", msgs[0].body())
        self.assertTrue(msgs[0].warnings)


class FoldingTest(unittest.TestCase):
    def test_folded_header_unfolds_without_loss(self):
        data = (
            SEP1 + b"\n"
            b"Subject: first part\n"
            b"\tsecond part\n"
            b" third part with leading space\n"
            b"Content-Type: text/plain; charset=utf-8\n"
            b"\nbody\n"
        )
        msg = split_bytes(data)[0]
        self.assertEqual(
            msg.get("Subject"),
            b" first part\tsecond part third part with leading space",
        )

    def test_long_folded_header_content_preserved(self):
        chunk_a = b"a" * 500
        chunk_b = b"b" * 500
        data = (
            b"X-Long: " + chunk_a + b"\n"
            b" " + chunk_b + b"\n\n"
        )
        msg = parse_one(io.BytesIO(data))
        value = bytes(msg.get("X-Long")).lstrip()
        # RFC 5322 unfolding removes only the CRLF; the WSP stays, so the
        # unfolded value is chunk_a + " " + chunk_b -- nothing is lost.
        self.assertEqual(value, chunk_a + b" " + chunk_b)
        self.assertEqual(len(value), 1001)

    def test_header_order_and_duplicates_preserved(self):
        data = b"X-One: 1\nX-Two: 2\nX-One: 3\n\nbody\n"
        msg = parse_one(io.BytesIO(data))
        self.assertEqual(msg.headers(),
                         [("X-One", b" 1"), ("X-Two", b" 2"), ("X-One", b" 3")])
        self.assertEqual(msg.get("X-One"), b" 1")  # first occurrence


class CharsetTest(unittest.TestCase):
    def test_valid_utf8_decodes(self):
        data = (
            b"Content-Type: text/plain; charset=utf-8\n\n"
            + "你好".encode("utf-8")
        )
        msg = parse_one(io.BytesIO(data))
        self.assertEqual(msg.body_text(), "你好")

    def test_declared_utf8_but_latin1_bytes_is_reported(self):
        data = b"Content-Type: text/plain; charset=utf-8\n\ncaf\xe9\n"
        msg = parse_one(io.BytesIO(data))
        with self.assertRaises(CharsetError):
            msg.body_text()
        # raw bytes remain accessible untouched -- nothing was replaced away
        self.assertEqual(msg.body(), b"caf\xe9")

    def test_unknown_charset_is_reported(self):
        data = b"Content-Type: text/plain; charset=x-not-a-codec\n\nabc\n"
        msg = parse_one(io.BytesIO(data))
        with self.assertRaises(CharsetError):
            msg.body_text()

    def test_declared_charset_actually_used(self):
        data = b"Content-Type: text/plain; charset=iso-8859-1\n\ncaf\xe9\n"
        msg = parse_one(io.BytesIO(data))
        self.assertEqual(msg.body_text(), "caf\u00e9")

    def test_header_name_non_ascii_is_reported_not_guessed(self):
        # A non-ASCII byte where a header name must be ASCII: the line is
        # demoted to body *with a warning* and its bytes are preserved.
        data = "Subject: ok\nS\xfcbj: bad\n\n".encode("latin-1")
        msg = parse_one(io.BytesIO(data))
        self.assertTrue(any("non-header line" in w for w in msg.warnings))
        self.assertIn(b"S\xfcbj: bad", msg.body())


class EscapeRoundTripTest(unittest.TestCase):
    def test_writer_reader_escape_roundtrip(self):
        for original in (
            b"From x Mon Oct  5 09:00:00 2026",
            b">From x Mon Oct  5 09:00:00 2026",
            b">>>From deeper",
            b"ordinary text",
            b"X-From: not at start",
        ):
            archived = mailsplit.escape_body_line(original)
            restored = archived[1:] if archived.startswith(b">From") or \
                archived.startswith(b">>From") else archived
            # simulate the reader regex directly
            if mailsplit._MBOXRD_UNESCAPE_RE.match(archived):
                restored = archived[1:]
            self.assertEqual(restored, original, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)

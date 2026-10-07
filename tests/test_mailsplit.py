"""Self-tests for mailsplit (unittest, stdlib only)."""

import io
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mailsplit import Message, SplitRule, parse_message, split_archive  # noqa: E402

MARKER = b"----MAIL-SEPARATOR----"


def split_bytes(data: bytes, rule: SplitRule = SplitRule()) -> list:
    return list(split_archive(io.BytesIO(data), rule))


def msg_text(headers, body=b""):
    out = b"\n".join(headers) + b"\n\n"
    if body:
        if not body.endswith(b"\n"):
            body += b"\n"
        out += body
    return out


class SplitterTests(unittest.TestCase):
    def test_single_message(self):
        raw = msg_text([b"From: a@x", b"Subject: hi", b"Date: x"], b"hello\n")
        msgs = split_bytes(MARKER + b"\n" + raw + b"\n")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].body, b"hello\n")
        self.assertEqual(msgs[0].header(b"subject"), b"hi")
        self.assertEqual([d for d in msgs[0].diagnostics
                          if d.startswith(("no-headers", "charset"))],
                         ["charset-missing: no charset declared; body left as raw bytes"])

    def test_single_message_without_leading_marker(self):
        raw = msg_text([b"From: a@x", b"Date: x", b"Subject: s"], b"x\n")
        msgs = split_bytes(raw)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].body, b"x\n")

    def test_empty_body(self):
        raw = msg_text([b"From: a@x", b"Date: x", b"Subject: s"])
        msgs = split_bytes(MARKER + b"\n" + raw + MARKER + b"\n" + raw)
        self.assertEqual(len(msgs), 2)
        for m in msgs:
            self.assertEqual(m.body, b"")

    def test_body_marker_line_mid_paragraph(self):
        # marker line in body, preceded by non-blank text -> not a separator
        body = b"line1\n" + MARKER + b"\nline3\n"
        raw = msg_text([b"From: a@x", b"Date: x", b"Subject: s"], body)
        msgs = split_bytes(MARKER + b"\n" + raw + b"\n")
        self.assertEqual(len(msgs), 1)
        self.assertIn(MARKER, msgs[0].body)

    def test_body_marker_after_blank_then_plain_text(self):
        body = b"para1\n\n" + MARKER + b"\nplain continuation, not a header\n"
        raw = msg_text([b"From: a@x", b"Date: x"], body)
        msgs = split_bytes(MARKER + b"\n" + raw + b"\n")
        self.assertEqual(len(msgs), 1)
        self.assertIn(MARKER + b"\nplain continuation", msgs[0].body)

    def test_body_marker_after_blank_then_one_headerlike_line(self):
        # Followed by only ONE header-like line: min_headers=2 protects.
        body = b"para1\n\n" + MARKER + b"\nNote: looks like a header\nmore text\n"
        raw = msg_text([b"From: a@x", b"Date: x", b"Subject: s"], body)
        msgs = split_bytes(MARKER + b"\n" + raw + b"\n")
        self.assertEqual(len(msgs), 1)

    def test_required_headers_protection(self):
        # Even two header-like lines follow: required From/Date still rejects.
        body = b"p\n\n" + MARKER + b"\nNote: a\nTip: b\nmore text\n"
        raw = msg_text([b"From: a@x", b"Date: x"], body)
        rule = SplitRule(required_headers=(b"from", b"date"))
        msgs = split_bytes(MARKER + b"\n" + raw + b"\n", rule)
        self.assertEqual(len(msgs), 1)

    def test_min_headers_one_accepts_headerlike_body(self):
        # Documented trade-off: weak rule (min_headers=1) -> false split when
        # a body marker line is followed by "X: y" + blank line.
        body = b"p\n\n" + MARKER + b"\nNote: a\n\nmore text\n"
        raw = msg_text([b"From: a@x", b"Date: x"], body)
        msgs = split_bytes(MARKER + b"\n" + raw + b"\n",
                           SplitRule(min_headers=1))
        self.assertEqual(len(msgs), 2)
        # ...while the default min_headers=2 still rejects it
        msgs = split_bytes(MARKER + b"\n" + raw + b"\n")
        self.assertEqual(len(msgs), 1)

    def test_header_block_must_be_blank_terminated(self):
        # "X: y" directly followed by plain text (no blank line) is not a
        # valid header block -> not a separator.
        body = b"p\n\n" + MARKER + b"\nNote: a\nplain text\n"
        raw = msg_text([b"From: a@x", b"Date: x"], body)
        msgs = split_bytes(MARKER + b"\n" + raw + b"\n",
                           SplitRule(min_headers=1))
        self.assertEqual(len(msgs), 1)

    def test_missing_headers_chunk(self):
        rule = SplitRule(allow_empty_headers=True)
        data = (MARKER + b"\n\nbody without headers line1\nline2\n\n"
                + MARKER + b"\n\nsecond headerless body\n")
        msgs = split_bytes(data, rule)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].headers, [])
        self.assertEqual(msgs[0].body, b"body without headers line1\nline2\n")
        self.assertTrue(any(d.startswith("no-headers") for d in msgs[0].diagnostics))
        self.assertEqual(msgs[1].body, b"second headerless body\n")

    def test_headerless_chunk_rejected_by_default(self):
        data = MARKER + b"\n\nbody only\n\n" + MARKER + b"\n\nbody2\n"
        msgs = split_bytes(data)  # allow_empty_headers=False
        self.assertEqual(len(msgs), 1)
        self.assertIn(b"body only", msgs[0].body)
        self.assertIn(b"body2", msgs[0].body)

    def test_folded_header_unfolded_without_loss(self):
        long_value = b"x" * 200
        folded = (b"Subject: start\n "
                  + long_value[:100] + b"\n\t" + long_value[100:] + b"\n end")
        raw = folded + b"\nFrom: a@x\nDate: x\n\nbody\n"
        msgs = split_bytes(raw)
        subj = msgs[0].header(b"subject")
        # unfolding removes the CRLF only; WSP and content are preserved
        expected = (b"start" + b" " + long_value[:100]
                    + b"\t" + long_value[100:] + b" end")
        self.assertEqual(subj, expected)
        self.assertEqual(len(subj), len(b"start") + 1 + 100 + 1 + 100 + 4)

    def test_orphan_continuation_reported(self):
        msg = parse_message([b" orphan line", b"From: a@x", b"Date: x",
                             b"", b"body\n"])
        self.assertTrue(any(d.startswith("orphan-continuation")
                            for d in msg.diagnostics))

    def test_body_ending_with_blank_line_roundtrip(self):
        # writer emits one extra blank as delimiter; body blank is preserved
        m1 = msg_text([b"From: a@x", b"Date: x"], b"text\n\n")
        m2 = msg_text([b"From: b@x", b"Date: y"], b"more\n")
        archive = MARKER + b"\n" + m1 + b"\n" + MARKER + b"\n" + m2
        msgs = split_bytes(archive)
        self.assertEqual(msgs[0].body, b"text\n\n")
        self.assertEqual(msgs[1].body, b"more\n")

    def test_escape_prefix(self):
        rule = SplitRule(escape_prefix=b">")
        body = b"quoted:\n" + b">" + MARKER + b"\nafter\n"
        m1 = msg_text([b"From: a@x", b"Date: x"], body)
        m2 = msg_text([b"From: b@x", b"Date: y"], b"two\n")
        archive = MARKER + b"\n" + m1 + b"\n" + MARKER + b"\n" + m2
        msgs = split_bytes(archive, rule)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].body,
                         b"quoted:\n" + MARKER + b"\nafter\n")


class EncodingTests(unittest.TestCase):
    def test_charset_missing_reported(self):
        m = parse_message([b"From: a@x", b"Date: x", b"", b"hi\n"])
        self.assertTrue(any(d.startswith("charset-missing") for d in m.diagnostics))

    def test_charset_decode_error_reported(self):
        m = parse_message([
            b"Content-Type: text/plain; charset=utf-8",
            b"Content-Transfer-Encoding: 8bit",
            b"", b"\xff\xfe invalid\n"])
        self.assertTrue(any(d.startswith("charset-decode-error")
                            for d in m.diagnostics))

    def test_ascii_charset_with_high_bytes(self):
        m = parse_message([
            b"Content-Type: text/plain; charset=us-ascii",
            b"", b"caf\xc3\xa9\n"])
        self.assertTrue(any(d.startswith("charset-decode-error")
                            for d in m.diagnostics))
        # raw bytes are preserved, nothing was replaced
        self.assertIn(b"\xc3\xa9", m.body)

    def test_unknown_charset_reported(self):
        m = parse_message([
            b"Content-Type: text/plain; charset=x-no-such-charset",
            b"", b"x\n"])
        self.assertTrue(any(d.startswith("unknown-charset") for d in m.diagnostics))

    def test_sevenbit_violation(self):
        m = parse_message([
            b"Content-Type: text/plain; charset=utf-8",
            b"Content-Transfer-Encoding: 7bit",
            b"", b"caf\xc3\xa9\n"])
        self.assertTrue(any(d.startswith("cte-7bit-violation")
                            for d in m.diagnostics))

    def test_base64_and_qp_valid(self):
        import base64, quopri
        text = "中文正文\n".encode("utf-8")
        m = parse_message([
            b"Content-Type: text/plain; charset=utf-8",
            b"Content-Transfer-Encoding: base64", b"",
            base64.b64encode(text), b""])
        self.assertFalse(any("error" in d for d in m.diagnostics))
        m2 = parse_message([
            b"Content-Type: text/plain; charset=utf-8",
            b"Content-Transfer-Encoding: quoted-printable", b"",
            quopri.encodestring(text)])
        self.assertFalse(any("error" in d for d in m2.diagnostics))

    def test_bad_base64_reported(self):
        m = parse_message([
            b"Content-Type: text/plain; charset=utf-8",
            b"Content-Transfer-Encoding: base64", b"", b"@@@@notb64\n"])
        self.assertTrue(any(d.startswith("cte-decode-error")
                            for d in m.diagnostics))


class RoundTripPropertyTests(unittest.TestCase):
    def test_random_archive_field_by_field(self):
        rng = random.Random(2630)
        n = 200
        singles = []
        chunks = []
        for i in range(n):
            headers = [b"From: user%d@example.com" % i,
                       b"Date: 2026-10-07",
                       b"Message-ID: <%d@t>" % i]
            subject = b"Subject: " + b"long " * rng.randrange(1, 30)
            if len(subject) > 70:
                subject = b"Subject: start\n " + subject[9:90] + b"\n " + subject[90:]
            headers.append(subject)
            body_lines = []
            if rng.random() > 0.15:
                for j in range(rng.randrange(0, 20)):
                    if rng.random() < 0.08:
                        # marker-like body line, always in a safe position
                        body_lines.append(MARKER)
                    elif rng.random() < 0.1:
                        body_lines.append(b"Note: header-like but isolated "
                                          + b"x" * rng.randrange(0, 40))
                    else:
                        body_lines.append(b"body line %d %s"
                                          % (j, b"y" * rng.randrange(0, 50)))
            if rng.random() < 0.2:
                body_lines.append(b"")  # body ends with a blank line
            ctype = (b"Content-Type: text/plain; charset=utf-8"
                     if rng.random() > 0.2
                     else b"Content-Type: text/plain")
            headers.append(ctype)
            body = b"\n".join(body_lines)
            if body and not body.endswith(b"\n"):
                body += b"\n"
            single = msg_text(headers, body)
            singles.append(single)
            chunks.append(MARKER + b"\n" + single + b"\n")
        msgs = split_bytes(b"".join(chunks))
        self.assertEqual(len(msgs), n)
        for single, got in zip(singles, msgs):
            want = parse_message(single.rstrip(b"\n").split(b"\n"))
            self.assertEqual(got.headers, want.headers)
            self.assertEqual(got.body, want.body)

    def test_crlf_normalised(self):
        data = (MARKER + b"\r\nFrom: a@x\r\nDate: x\r\n\r\n"
                b"line1\r\n" + MARKER + b"\r\nbody-marker-safe\r\n\r\n"
                + MARKER + b"\r\nFrom: b@x\r\nDate: y\r\n\r\nbody2\r\n")
        msgs = split_bytes(data)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].body,
                         b"line1\n" + MARKER + b"\nbody-marker-safe\n")
        self.assertEqual(msgs[1].body, b"body2\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)

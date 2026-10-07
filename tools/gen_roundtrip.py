"""Generate round-trip fixture data: single .eml files + combined archives.

Usage: python3 tools/gen_roundtrip.py [outdir]   (default: data/roundtrip)
"""
import base64
import os
import quopri
import sys

MARKER = b"----MAIL-SEPARATOR----"


def fold(header_line: bytes, limit: int = 72) -> bytes:
    """RFC 5322 style folding of an over-long header line."""
    out = []
    line = header_line
    while len(line) > limit:
        cut = line.rfind(b" ", 0, limit)
        if cut <= 0:
            cut = limit
        out.append(line[:cut])
        line = b" " + line[cut + 1:]
    out.append(line)
    return b"\n".join(out)


def build_messages():
    """Return list of (name, message_bytes); each ends with exactly one \\n."""
    msgs = []

    msgs.append(("001-plain", b"\n".join([
        b"From: alice@example.com",
        b"To: bob@example.com",
        b"Date: Tue, 07 Oct 2026 09:00:00 +0800",
        b"Subject: plain ascii message",
        b"Content-Type: text/plain; charset=us-ascii",
        b"",
        b"Hello Bob,",
        b"this is a plain message.",
        b""])))

    long_subj = (b"Subject: " + b"a very long subject line that must be folded "
                 b"because it exceeds seventy eight characters by quite a bit "
                 b"and keeps going to prove unfolding loses nothing")
    msgs.append(("002-folded", b"\n".join([
        b"From: carol@example.com",
        b"Date: Tue, 07 Oct 2026 09:01:00 +0800",
        fold(long_subj),
        b"X-Custom: one",
        fold(b"X-Long: " + b"word " * 60),
        b"Content-Type: text/plain; charset=utf-8",
        b"",
        "正文包含中文与 emoji \U0001f600，验证 utf-8 解码。".encode("utf-8"),
        b""])))

    msgs.append(("003-empty-body", b"\n".join([
        b"From: dave@example.com",
        b"Date: Tue, 07 Oct 2026 09:02:00 +0800",
        b"Subject: no body at all",
        b"Content-Type: text/plain; charset=utf-8",
        b"",
        b""])))

    body_with_marker = b"\n".join([
        b"Here is a line, then the marker text appears inside the body:",
        MARKER,                       # mid-paragraph: previous line not blank
        b"and text continues right after it.",
        b"",
        MARKER,                       # after blank, but next line is not a header
        b"not a header line at all",
        b"",
        MARKER,                       # after blank, ONE header-like line only
        b"Note: single header-like line is not enough (min_headers=2)",
        b"trailing text",
        b"",
        MARKER,                       # header-like lines not blank-terminated
        b"Looks: like headers",
        b"And: two of them",
        b"but then plain text with no blank line terminator",
        b""])
    msgs.append(("004-marker-in-body", b"\n".join([
        b"From: erin@example.com",
        b"Date: Tue, 07 Oct 2026 09:03:00 +0800",
        b"Subject: body contains separator-like lines",
        b"Content-Type: text/plain; charset=utf-8",
        b"",
        body_with_marker])))

    qp = quopri.encodestring("quoted-printable 中文内容\n第二行\n".encode("utf-8"))
    msgs.append(("005-qp", b"\n".join([
        b"From: frank@example.com",
        b"Date: Tue, 07 Oct 2026 09:04:00 +0800",
        b"Subject: quoted printable",
        b"Content-Type: text/plain; charset=utf-8",
        b"Content-Transfer-Encoding: quoted-printable",
        b"",
        qp.rstrip(b"\n"),
        b""])))

    b64 = base64.encodebytes("base64 编码的中文正文\n".encode("utf-8")).rstrip(b"\n")
    msgs.append(("006-base64", b"\n".join([
        b"From: grace@example.com",
        b"Date: Tue, 07 Oct 2026 09:05:00 +0800",
        b"Subject: base64 body",
        b"Content-Type: text/plain; charset=utf-8",
        b"Content-Transfer-Encoding: base64",
        b"",
        b64,
        b""])))

    msgs.append(("007-no-charset", b"\n".join([
        b"From: heidi@example.com",
        b"Date: Tue, 07 Oct 2026 09:06:00 +0800",
        b"Subject: charset not declared (expect charset-missing diagnostic)",
        b"",
        b"Body without any Content-Type header.",
        b""])))

    msgs.append(("008-charset-mismatch", b"\n".join([
        b"From: ivan@example.com",
        b"Date: Tue, 07 Oct 2026 09:07:00 +0800",
        b"Subject: declares utf-8 but is latin-1 (expect charset-decode-error)",
        b"Content-Type: text/plain; charset=utf-8",
        b"Content-Transfer-Encoding: 8bit",
        b"",
        b"caf\xe9 na\xefve",   # latin-1 bytes, invalid utf-8
        b""])))

    msgs.append(("009-body-ends-blank", b"\n".join([
        b"From: judy@example.com",
        b"Date: Tue, 07 Oct 2026 09:08:00 +0800",
        b"Subject: body ends with a blank line",
        b"Content-Type: text/plain; charset=us-ascii",
        b"",
        b"body text",
        b"",
        b""])))

    msgs.append(("010-crlf-body-tabs", b"\n".join([
        b"From: mallory@example.com",
        b"Date: Tue, 07 Oct 2026 09:09:00 +0800",
        b"Subject: tabs and trailing spaces\t ",
        b"Content-Type: text/plain; charset=us-ascii",
        b"",
        b"indented\tline",
        b"trailing spaces   ",
        b""])))
    return msgs


def build_headerless_messages():
    return [
        ("h01", b"\nbody of a message that has no headers at all\nsecond line\n"),
        ("h02", b"\nanother headerless body\n"),
    ]


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "roundtrip")
    singles = os.path.join(outdir, "singles")
    os.makedirs(singles, exist_ok=True)

    msgs = build_messages()
    with open(os.path.join(outdir, "archive.txt"), "wb") as arch:
        for name, data in msgs:
            with open(os.path.join(singles, name + ".eml"), "wb") as f:
                f.write(data)
            arch.write(MARKER + b"\n" + data + b"\n")  # blank = delimiter

    hmsgs = build_headerless_messages()
    with open(os.path.join(outdir, "archive-headerless.txt"), "wb") as arch:
        for name, data in hmsgs:
            with open(os.path.join(singles, name + ".eml"), "wb") as f:
                f.write(data)
            arch.write(MARKER + b"\n" + data + b"\n")

    print("wrote %d + %d messages to %s" % (len(msgs), len(hmsgs), outdir))


if __name__ == "__main__":
    main()

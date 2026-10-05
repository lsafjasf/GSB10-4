"""Build the golden data set (对拍数据).

Creates fixtures/messages/*.eml (single-message originals) and
fixtures/archive.mbox (all messages concatenated with separators, body lines
that could look like separators are mboxrd-escaped).

Deterministic: no randomness, no wall clock.
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
MSG_DIR = os.path.join(HERE, "fixtures", "messages")
ARCHIVE = os.path.join(HERE, "fixtures", "archive.mbox")

# (filename, sender, date-for-separator, raw message bytes)
MESSAGES = [
    (
        "01_simple.eml",
        "alice@example.com",
        "Mon Oct  5 09:00:00 2026",
        b"From: Alice <alice@example.com>\n"
        b"To: bob@example.com\n"
        b"Subject: Hello\n"
        b"Content-Type: text/plain; charset=utf-8\n"
        b"\n"
        b"Hi Bob,\n"
        b"see you at 10.\n"
        b"-- Alice\n",
    ),
    (
        "02_folded_headers.eml",
        "carol@example.com",
        "Mon Oct  5 09:05:00 2026",
        b"From: Carol <carol@example.com>\n"
        b"Subject: A very long subject line that was folded by the sender's\n"
        b"\tmail user agent across two physical lines\n"
        b"X-Custom-Long: " + b"x" * 120 + b"\n"
        b"\tcontinued here without losing a single byte\n"
        b"Content-Type: text/plain; charset=utf-8\n"
        b"\n"
        b"folded headers must survive\n",
    ),
    (
        "03_empty_body.eml",
        "dan@example.com",
        "Mon Oct  5 09:10:00 2026",
        b"From: Dan <dan@example.com>\n"
        b"Subject: no body\n"
        b"\n",
    ),
    (
        "04_separator_in_body.eml",
        "erin@example.com",
        "Mon Oct  5 09:15:00 2026",
        b"From: Erin <erin@example.com>\n"
        b"Subject: quoting a log\n"
        b"Content-Type: text/plain; charset=utf-8\n"
        b"\n"
        b"The mbox file contained this line verbatim:\n"
        b"From mallory@evil.example Sat Nov  1 03:04:05 2025\n"
        b"and this one too:\n"
        b"From nobody@nowhere.invalid Tue Dec 31 23:59:59 2030\n"
        b"but both are just quoted log output, not boundaries.\n",
    ),
    (
        "05_no_headers.eml",
        "frank@example.com",
        "Mon Oct  5 09:20:00 2026",
        b"\n"
        b"This message has no header section at all.\n"
        b"It starts directly with a blank line.\n",
    ),
    (
        "06_utf8_body.eml",
        "gwen@example.com",
        "Mon Oct  5 09:25:00 2026",
        "From: Gwen <gwen@example.com>\n"
        "Subject: =?UTF-8?B?5L2g5aW977yM5LiW55WM?=\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "\n"
        "你好，世界！附件是《拆分规则》终稿。\n".encode("utf-8"),
    ),
    (
        "07_charset_mismatch.eml",
        "heidi@example.com",
        "Mon Oct  5 09:30:00 2026",
        b"From: Heidi <heidi@example.com>\n"
        b"Subject: declared utf-8 but actually latin-1\n"
        b"Content-Type: text/plain; charset=utf-8\n"
        b"\n"
        b"caf\xe9 na\xefve \xff\n",   # invalid UTF-8 on purpose
    ),
    (
        "08_crlf_and_gtfrom.eml",
        "ivan@example.com",
        "Mon Oct  5 09:35:00 2026",
        b"From: Ivan <ivan@example.com>\r\n"
        b"Subject: CRLF + >From\r\n"
        b"Content-Type: text/plain; charset=us-ascii\r\n"
        b"\r\n"
        b">From a previous quoted message\r\n"
        b">>From doubly quoted\r\n"
        b"done\r\n",
    ),
]


def main() -> None:
    import mailsplit

    os.makedirs(MSG_DIR, exist_ok=True)
    with open(ARCHIVE, "wb") as archive:
        for filename, sender, date, raw in MESSAGES:
            with open(os.path.join(MSG_DIR, filename), "wb") as fh:
                fh.write(raw)
            archive.write(f"From {sender} {date}\n".encode("ascii"))
            for line in raw.split(b"\n")[:-1]:  # drop trailing "" from final \n
                line = line.rstrip(b"\r")
                # header section (before first blank) is written verbatim;
                # body lines get mboxrd escaping.
                archive.write(mailsplit.escape_body_line(line) + b"\n")
    print(f"wrote {len(MESSAGES)} messages -> {MSG_DIR}")
    print(f"wrote archive -> {ARCHIVE}")


if __name__ == "__main__":
    main()

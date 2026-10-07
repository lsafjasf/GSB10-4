"""Field-by-field comparison: split(archive) vs original single .eml files.

Usage: python3 tools/check_roundtrip.py [datadir]   (default: data/roundtrip)
Exit code 0 iff every message matches field by field.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mailsplit import SplitRule, parse_message, split_archive  # noqa: E402

MARKER = b"----MAIL-SEPARATOR----"


def parse_file(path):
    with open(path, "rb") as f:
        data = f.read()
    if data.endswith(b"\n"):
        data = data[:-1]  # strip the single line-terminating newline only
    lines = data.split(b"\n")
    return parse_message(lines)


def compare(want, got, label, failures):
    for field_w, field_g, what in (
        (want.headers, got.headers, "headers"),
        (want.body, got.body, "body"),
        (sorted(want.diagnostics), sorted(got.diagnostics), "diagnostics"),
    ):
        if field_w != field_g:
            failures.append("%s: %s differ\n  want=%r\n  got =%r"
                            % (label, what, field_w, field_g))


def run_archive(datadir, archive_name, names, rule, failures):
    with open(os.path.join(datadir, archive_name), "rb") as f:
        got_msgs = list(split_archive(f, rule))
    if len(got_msgs) != len(names):
        failures.append("%s: message count %d != %d"
                        % (archive_name, len(got_msgs), len(names)))
        return
    for name, got in zip(names, got_msgs):
        want = parse_file(os.path.join(datadir, "singles", name + ".eml"))
        compare(want, got, "%s/%s" % (archive_name, name), failures)


def main():
    datadir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "roundtrip")
    failures = []
    run_archive(datadir, "archive.txt",
                ["001-plain", "002-folded", "003-empty-body",
                 "004-marker-in-body", "005-qp", "006-base64",
                 "007-no-charset", "008-charset-mismatch",
                 "009-body-ends-blank", "010-crlf-body-tabs"],
                SplitRule(marker=MARKER), failures)
    run_archive(datadir, "archive-headerless.txt", ["h01", "h02"],
                SplitRule(marker=MARKER, allow_empty_headers=True), failures)
    if failures:
        print("ROUND-TRIP FAILED:")
        for f_ in failures:
            print(" ", f_)
        return 1
    print("ROUND-TRIP OK: 12/12 messages identical field by field "
          "(headers, body, diagnostics)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

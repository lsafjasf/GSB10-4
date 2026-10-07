"""CLI: python -m mailsplit {split,count} ARCHIVE [options]"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter

from .core import SplitRule, split_archive


def _add_rule_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("archive", help="archive file path ('-' for stdin)")
    p.add_argument("--marker", default="----MAIL-SEPARATOR----",
                   help="exact content of the separator line")
    p.add_argument("--min-headers", type=int, default=2,
                   help="valid header lines required after the marker (default 2)")
    p.add_argument("--allow-empty-headers", action="store_true",
                   help="accept 'marker + blank line' as a separator (weaker)")
    p.add_argument("--required-header", action="append", default=[],
                   help="field name that must follow the marker (repeatable)")
    p.add_argument("--escape-prefix", default=None,
                   help="unescape '<prefix><marker>' body lines (mbox-style)")
    p.add_argument("--no-require-blank-before", action="store_true",
                   help="do not require a blank line before the marker")


def _rule(a: argparse.Namespace) -> SplitRule:
    return SplitRule(
        marker=a.marker.encode("utf-8"),
        min_headers=a.min_headers,
        require_blank_before=not a.no_require_blank_before,
        allow_empty_headers=a.allow_empty_headers,
        required_headers=tuple(h.encode("utf-8") for h in a.required_header),
        escape_prefix=a.escape_prefix.encode("utf-8") if a.escape_prefix else None,
    )


def _open(path: str):
    return sys.stdin.buffer if path == "-" else open(path, "rb")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mailsplit", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_split = sub.add_parser("split", help="split archive into one file per message")
    _add_rule_args(p_split)
    p_split.add_argument("-o", "--out", required=True, help="output directory")
    p_count = sub.add_parser("count", help="count messages (streaming, no output files)")
    _add_rule_args(p_count)
    args = ap.parse_args(argv)

    rule = _rule(args)
    t0 = time.perf_counter()
    diags: Counter = Counter()
    count = 0
    with _open(args.archive) as fh:
        if args.cmd == "split":
            os.makedirs(args.out, exist_ok=True)
            for msg in split_archive(fh, rule):
                count += 1
                for d in msg.diagnostics:
                    diags[d.split(":")[0]] += 1
                name = os.path.join(args.out, "msg-%05d.eml" % msg.index)
                with open(name, "wb") as out:
                    out.write(msg.raw)
        else:
            for msg in split_archive(fh, rule):
                count += 1
                for d in msg.diagnostics:
                    diags[d.split(":")[0]] += 1
    dt = time.perf_counter() - t0
    print("messages: %d" % count)
    print("elapsed:  %.3f s" % dt)
    if diags:
        print("diagnostics:")
        for kind, n in sorted(diags.items()):
            print("  %-32s %d" % (kind, n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI: python3 -m icon_theme THEME_ROOT {resolve NAME SIZE CONTEXT|validate}"""

import argparse
import sys

from .theme import Resolver
from .validate import validate_theme


def main(argv=None):
    parser = argparse.ArgumentParser(prog="icon_theme")
    parser.add_argument("theme_root")
    sub = parser.add_subparsers(dest="cmd", required=True)
    res = sub.add_parser("resolve", help="resolve one icon query")
    res.add_argument("name")
    res.add_argument("size", type=int)
    res.add_argument("context")
    sub.add_parser("validate", help="validate theme directory conventions")
    args = parser.parse_args(argv)

    if args.cmd == "resolve":
        r = Resolver(args.theme_root).resolve(args.name, args.size, args.context)
        print(f"status: {r.status}")
        if r.hit:
            print(f"hit: {r.hit.describe()}")
        for c in r.corrupt:
            print(f"corrupt: {c.path} ({c.reason}) "
                  f"[stage={c.stage} L{c.level} {c.level_name}]")
        print("chain:")
        for e in r.chain:
            print(f"  {e}")
        return 0 if r.status == "hit" else 1

    report = validate_theme(args.theme_root)
    print(f"theme: {report.root}")
    print(f"size dirs: {report.size_dirs}  icon files: {report.icon_files}  "
          f"dirs scanned: {report.dirs_scanned}")
    for p in report.problems:
        print(f"  {p}")
    for c in report.corrupt:
        print(f"  [corrupt] {c.path}: {c.reason}")
    print(f"result: {'OK' if report.ok() else 'PROBLEMS'} "
          f"({len(report.errors())} errors, {len(report.warnings())} warnings, "
          f"{len(report.corrupt)} corrupt files)")
    return 0 if report.ok() else 1


if __name__ == "__main__":
    sys.exit(main())

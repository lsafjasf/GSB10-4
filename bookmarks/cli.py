"""命令行入口：

    python3 -m bookmarks check     FILE                 校验并输出异常报告
    python3 -m bookmarks roundtrip IN OUT [--report R]  解析后原样写回并对比
    python3 -m bookmarks stats     FILE                 输出节点数/深度等统计

退出码：0 = 无异常；1 = 有异常（已修复并报告）；2 = 致命错误（无法解析）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

from .iterjson import JSONParseError
from .model import BookmarkDocument, BookmarkFormatError


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read()


def _load(path: str) -> BookmarkDocument:
    return BookmarkDocument.from_text(_read(path))


def _print_fatal(exc: Exception) -> None:
    print(f"致命错误：{exc}", file=sys.stderr)


def _cmd_check(args) -> int:
    doc = _load(args.file)
    stats = doc.stats()
    print(f"节点 {stats['nodes']} 个（目录 {stats['folders']} / 链接 {stats['urls']}），"
          f"最大深度 {stats['max_depth']}")
    print(doc.report.to_text(), end="")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(doc.report.to_dict(), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"异常报告已写入 {args.report}")
    return 0 if doc.report.ok else 1


def _cmd_roundtrip(args) -> int:
    src = _read(args.input)
    doc = BookmarkDocument.from_text(src)
    out = doc.to_text()
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(out)
    in_hash = hashlib.sha256(src.encode("utf-8")).hexdigest()
    out_hash = hashlib.sha256(out.encode("utf-8")).hexdigest()
    identical = src == out
    print(f"输入  sha256: {in_hash}")
    print(f"输出  sha256: {out_hash}")
    if identical:
        print("往返结果：字节完全一致（无损）")
    else:
        print("往返结果：存在差异（原因见下方异常报告，通常为已修复的异常）")
    print(doc.report.to_text(), end="")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(doc.report.to_dict(), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"异常报告已写入 {args.report}")
    return 0 if identical else 1


def _cmd_stats(args) -> int:
    doc = _load(args.file)
    print(json.dumps(doc.stats(), ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="bookmarks", description="书签解析与还原工具（gsb-bookmarks v1）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="校验书签文件并输出异常报告")
    p_check.add_argument("file")
    p_check.add_argument("--report", help="把 JSON 格式异常报告写入指定文件")
    p_check.set_defaults(func=_cmd_check)

    p_rt = sub.add_parser("roundtrip", help="解析后原样写回并做字节对比")
    p_rt.add_argument("input")
    p_rt.add_argument("output")
    p_rt.add_argument("--report", help="把 JSON 格式异常报告写入指定文件")
    p_rt.set_defaults(func=_cmd_roundtrip)

    p_stats = sub.add_parser("stats", help="输出节点数、深度等统计信息")
    p_stats.add_argument("file")
    p_stats.set_defaults(func=_cmd_stats)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (JSONParseError, BookmarkFormatError) as exc:
        _print_fatal(exc)
        return 2
    except OSError as exc:
        _print_fatal(exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

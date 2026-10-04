"""命令行入口：python3 -m logshard.cli <merge|inspect> ..."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from .reader import Merger, discover_shards


def _report_dict(report) -> dict:
    def conv(obj):
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="backslashreplace")
        if isinstance(obj, tuple):
            return list(obj)
        return obj

    def to_jsonable(value):
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {k: to_jsonable(v) for k, v in dataclasses.asdict(value).items()}
        if isinstance(value, list):
            return [to_jsonable(v) for v in value]
        if isinstance(value, dict):
            return {k: to_jsonable(v) for k, v in value.items()}
        return conv(value)

    return to_jsonable(report)


def cmd_merge(args: argparse.Namespace) -> int:
    merger = Merger(args.shards, max_shard_bytes=args.max_shard_bytes)
    out = open(args.out, "wb") if args.out else sys.stdout.buffer
    try:
        for rec in merger.records():
            out.write(rec.raw)
    finally:
        if args.out:
            out.close()
    report = merger.report
    report_json = _report_dict(report)
    if args.report:
        Path(args.report).write_text(json.dumps(report_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "shards": report.shards_present,
        "segments": report.segments,
        "records": report.records,
        "gaps": len(report.gaps),
        "dangling_tails": len(report.dangling_tails),
        "dangling_heads": len(report.dangling_heads),
        "truncated_tails": len(report.truncated_tails),
        "rollbacks": len(report.rollbacks),
        "duplicate_seqs": len(report.duplicate_seqs),
        "parse_errors": len(report.parse_errors),
    }
    print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)
    if report.gaps or report.truncated_tails or report.parse_errors:
        return 3 if args.strict else 0
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    metas = discover_shards(args.shards)
    info = [
        {"path": str(m.path), "offset": m.offset, "size": m.size, "end": m.end}
        for m in sorted(metas, key=lambda m: m.offset)
    ]
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="logshard", description="日志分片轮转合并工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_merge = sub.add_parser("merge", help="合并分片，输出重组后的记录流")
    p_merge.add_argument("shards", nargs="+", help="分片文件（任意顺序，按文件头 offset 排序）")
    p_merge.add_argument("-o", "--out", help="输出文件（默认 stdout）")
    p_merge.add_argument("--report", help="缺口/异常报告 JSON 输出路径")
    p_merge.add_argument("--max-shard-bytes", type=int, default=None, help="轮转阈值，用于估算缺失分片数下界")
    p_merge.add_argument("--strict", action="store_true", help="存在缺口或截断时以退出码 3 失败")
    p_merge.set_defaults(func=cmd_merge)

    p_inspect = sub.add_parser("inspect", help="列出分片 offset 区间（验证不依赖文件名排序）")
    p_inspect.add_argument("shards", nargs="+")
    p_inspect.set_defaults(func=cmd_inspect)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

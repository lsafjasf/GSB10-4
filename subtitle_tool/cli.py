"""命令行入口：合并 / 整段偏移 / 分段偏移。

用法：
  python -m subtitle_tool.cli merge  a.srt b.srt -o out.srt [--report r.json]
  python -m subtitle_tool.cli shift  in.srt --offset -1500 -o out.srt [--no-strict]
  python -m subtitle_tool.cli shift  in.srt --segments seg.json -o out.srt
seg.json 格式: [{"start": 5000, "end": 10000, "offset_ms": -2000}, ...]（毫秒）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import (ShiftViolationError, ShiftWindow, merge_sources,
                   shift_global, shift_segmented)
from .srtio import parse_srt, render_srt


def _read(path: str) -> list:
    return parse_srt(Path(path).read_text(encoding="utf-8-sig"), tag=Path(path).name)


def _write_reports(reports, path: str | None) -> None:
    data = [r.to_dict() for r in reports]
    if path:
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    if data:
        print(f"[report] {len(data)} 条报告", file=sys.stderr)
        for r in data:
            c = r["cue"]
            print(f"  - {r['kind']}: [{c['start']}, {c['end']}) "
                  f"{c['text']!r} {r['detail']}", file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="subtitle_tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("merge", help="合并多个 SRT")
    pm.add_argument("inputs", nargs="+")
    pm.add_argument("-o", "--output", required=True)
    pm.add_argument("--report")

    ps = sub.add_parser("shift", help="偏移（整段或分段）")
    ps.add_argument("input")
    ps.add_argument("-o", "--output", required=True)
    ps.add_argument("--offset", type=int, help="整段平移毫秒数（可负）")
    ps.add_argument("--segments", help="分段平移 JSON 文件")
    ps.add_argument("--no-strict", action="store_true",
                    help="越界不报错，记入报告并保持原样")
    ps.add_argument("--report")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "merge":
            result = merge_sources([_read(p) for p in args.inputs])
            cues, reports = result.cues, result.reports
        else:
            cues_in = _read(args.input)
            strict = not args.no_strict
            if args.segments:
                windows = [ShiftWindow(w["start"], w["end"], w["offset_ms"])
                           for w in json.loads(Path(args.segments).read_text())]
                res = shift_segmented(cues_in, windows, strict=strict)
            elif args.offset is not None:
                res = shift_global(cues_in, args.offset, strict=strict)
            else:
                ap.error("shift 需要 --offset 或 --segments")
            cues, reports = res.cues, res.reports
    except ShiftViolationError as exc:
        _write_reports(exc.reports, args.report)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    Path(args.output).write_text(render_srt(cues), encoding="utf-8")
    _write_reports(reports, args.report)
    print(f"ok: {len(cues)} 条 -> {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

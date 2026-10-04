"""submerge: SRT subtitle merge and time-shift library (Python 3 stdlib only).

Timeline model
--------------
* Every cue is a half-open interval [start, end) on the integer-millisecond
  timeline. Half-open means a cue ending exactly where the next one starts
  (e.g. [1s,2s) + [2s,3s]) does NOT overlap and is left as two cues.
* Zero/negative duration cues (end <= start) describe an empty interval and
  are dropped during merge / rejected during shift; they are reported, never
  silently fixed.

Merge rule
----------
All cues are sorted by (start, end, source-order). Sweeping left to right, a
cue is joined to the current output cue whenever it overlaps it, and chains
are closed transitively (A~B and B~C merges A,B,C even if A and C do not
touch). The merged cue spans [min start, max end); texts are concatenated in
sweep order with a newline. Result: output cues are sorted, pairwise disjoint
(no nesting / no overlap of any kind). See MERGE_RULES.md.

Shift rules
-----------
* shift_cues(delta): translate every cue by the same delta.
* shift_by_segments(segments): the shift of a cue is chosen by the segment
  containing its START time. Segments are half-open and must not overlap each
  other; cues whose start lies outside every segment are untouched.
Any cue that would start at a negative time is an out-of-bounds violation:
all violations are collected and raised together (nothing is truncated and
the input is returned unchanged). Segment shifts may create new overlaps
between segments, so call merge_cues() again afterwards.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence, Tuple

__all__ = [
    "Cue",
    "Segment",
    "MergeResult",
    "SubtitleError",
    "OutOfBoundsError",
    "parse_time",
    "format_time",
    "parse_srt",
    "format_srt",
    "merge_cues",
    "shift_cues",
    "shift_by_segments",
]

TIME_RE = re.compile(r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")


@dataclass
class Cue:
    """A subtitle line visible on the half-open interval [start, end)."""

    start: int  # ms, inclusive
    end: int  # ms, exclusive
    text: str
    src: str = ""  # origin label (file:index), used in reports

    @property
    def duration(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Segment:
    """Half-open [start, end) window whose cues receive delta milliseconds."""

    start: int
    end: int
    delta: int


class SubtitleError(Exception):
    pass


class OutOfBoundsError(SubtitleError):
    """Raised when a shift would move cues across the 00:00:00,000 boundary."""

    def __init__(self, violations: Sequence[Tuple[Cue, int, int]]):
        self.violations = violations  # (cue, new_start_ms, new_end_ms)
        super().__init__(
            f"{len(violations)} cue(s) would start before 00:00:00,000; "
            "nothing was written"
        )


@dataclass
class MergeResult:
    cues: List[Cue] = field(default_factory=list)
    dropped: List[Cue] = field(default_factory=list)  # empty intervals


def parse_time(value: str) -> int:
    """Parse 'H:MM:SS,mmm' (comma or dot decimal separator) to milliseconds."""
    m = TIME_RE.fullmatch(value.strip())
    if not m:
        raise SubtitleError(f"bad timestamp: {value!r}")
    h, mm, ss, frac = m.groups()
    return (int(h) * 3600 + int(mm) * 60 + int(ss)) * 1000 + int(
        frac.ljust(3, "0")
    )


def format_time(ms: int) -> str:
    if ms < 0:
        raise SubtitleError(f"cannot format negative time: {ms}ms")
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def parse_srt(content: str, src: str = "") -> List[Cue]:
    """Parse SRT text. The numeric cue counter line is optional."""
    cues: List[Cue] = []
    for raw_block in re.split(r"\r?\n\r?\n+", content.strip()):
        lines = [ln.rstrip("\r") for ln in raw_block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        if "-->" not in lines[0] and len(lines) >= 2 and "-->" in lines[1]:
            lines = lines[1:]  # drop leading counter
        if "-->" not in lines[0]:
            raise SubtitleError(f"bad cue block in {src or '<input>'}: {raw_block!r}")
        left, _, right = lines[0].partition("-->")
        cues.append(
            Cue(
                start=parse_time(left),
                end=parse_time(right),
                text="\n".join(lines[1:]).strip(),
                src=f"{src}#{len(cues) + 1}" if src else f"#{len(cues) + 1}",
            )
        )
    return cues


def format_srt(cues: Iterable[Cue]) -> str:
    blocks = []
    for i, cue in enumerate(cues, 1):
        blocks.append(
            f"{i}\n{format_time(cue.start)} --> {format_time(cue.end)}\n{cue.text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def merge_cues(*cue_lists: Sequence[Cue]) -> MergeResult:
    """Merge any number of cue lists into one overlap-free, sorted list."""
    indexed: List[Tuple[int, int, Cue]] = []
    for origin, cues in enumerate(cue_lists):
        for order, cue in enumerate(cues):
            indexed.append((origin, order, cue))

    dropped = [c for _, _, c in indexed if c.end <= c.start]
    live = [item for item in indexed if item[2].end > item[2].start]
    live.sort(key=lambda item: (item[2].start, item[2].end, item[0], item[1]))

    merged: List[Cue] = []
    for _, _, cue in live:
        if merged and cue.start < merged[-1].end:  # overlap on half-open axis
            top = merged[-1]
            top.end = max(top.end, cue.end)
            top.text = top.text + "\n" + cue.text if top.text else cue.text
        else:
            merged.append(Cue(cue.start, cue.end, cue.text, cue.src))
    return MergeResult(cues=merged, dropped=dropped)


def _check_duration(cues: Sequence[Cue]) -> None:
    bad = [c for c in cues if c.end <= c.start]
    if bad:
        raise SubtitleError(
            f"{len(bad)} zero/negative-duration cue(s) cannot be shifted; "
            f"first offender: {bad[0].src}"
        )


def shift_cues(cues: Sequence[Cue], delta_ms: int) -> List[Cue]:
    """Translate every cue by delta_ms. Raises OutOfBoundsError, never truncates."""
    _check_duration(cues)
    violations = [
        (cue, cue.start + delta_ms, cue.end + delta_ms)
        for cue in cues
        if cue.start + delta_ms < 0
    ]
    if violations:
        raise OutOfBoundsError(violations)
    return [Cue(c.start + delta_ms, c.end + delta_ms, c.text, c.src) for c in cues]


def shift_by_segments(
    cues: Sequence[Cue], segments: Sequence[Segment]
) -> List[Cue]:
    """Shift each cue by the delta of the segment containing cue.start.

    Segments must be sorted and pairwise disjoint (validated here).
    """
    _check_duration(cues)
    segs = sorted(segments, key=lambda s: s.start)
    for a, b in zip(segs, segs[1:]):
        if b.start < a.end:
            raise SubtitleError(
                f"segments overlap: [{format_time(a.start)}, {format_time(a.end)}) "
                f"and [{format_time(b.start)}, {format_time(b.end)})"
            )

    def delta_for(start_ms: int) -> int:
        for seg in segs:
            if start_ms < seg.start:
                return 0
            if start_ms < seg.end:
                return seg.delta
        return 0

    shifted = [(cue, cue.start + delta_for(cue.start)) for cue in cues]
    violations = [(cue, ns, ns + cue.duration) for cue, ns in shifted if ns < 0]
    if violations:
        raise OutOfBoundsError(violations)
    return [Cue(ns, ns + cue.duration, cue.text, cue.src) for cue, ns in shifted]


# --------------------------------------------------------------------------- CLI


def _parse_delta(token: str) -> int:
    """'2.5' or '00:00:02,500' -> milliseconds."""
    token = token.strip()
    if ":" in token:
        return parse_time(token)
    return int(round(float(token) * 1000))


def _parse_segments(spec: str) -> List[Segment]:
    segs = []
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(
            r"(\d+:\d{1,2}:\d{1,2}[,.]\d{1,3})\s*,\s*"
            r"(\d+:\d{1,2}:\d{1,2}[,.]\d{1,3})\s*,\s*"
            r"(-?\d+(?:\.\d+)?)",
            part,
        )
        if not m:
            raise SubtitleError(f"bad segment spec (need start,end,delta): {part}")
        segs.append(
            Segment(
                parse_time(m.group(1)),
                parse_time(m.group(2)),
                _parse_delta(m.group(3)),
            )
        )
    return segs


def _oob_report(err: OutOfBoundsError) -> str:
    lines = [
        "OUT-OF-BOUNDS REPORT",
        f"violations: {len(err.violations)}",
        "the shift was rejected; output file was NOT written and no cue was "
        "truncated",
        "",
        "cue             new_start        new_end          original_interval",
    ]
    for cue, ns, ne in err.violations:
        ns_display = f"{ns}ms" if ns < 0 else f"{format_time(ns)} ({ns}ms)"
        lines.append(
            f"{cue.src:<32}{ns_display:<22}end {ne}ms  "
            f"[{format_time(cue.start)}, {format_time(cue.end)})"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="merge / shift SRT subtitles")
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("merge", help="merge two or more SRT files")
    pm.add_argument("output")
    pm.add_argument("inputs", nargs="+")

    ps = sub.add_parser("shift", help="uniform or segmented time shift")
    ps.add_argument("output")
    ps.add_argument("input")
    ps.add_argument("--delta", help="uniform shift in seconds (e.g. -2.5)")
    ps.add_argument(
        "--segments",
        help="start,end,delta triples (times HH:MM:SS,mmm; delta seconds), "
        "joined by ';'",
    )
    args = p.parse_args(argv)

    try:
        if args.cmd == "merge":
            all_cues = []
            for path in args.inputs:
                with open(path, encoding="utf-8-sig") as fh:
                    all_cues.append(parse_srt(fh.read(), src=path))
            result = merge_cues(*all_cues)
            for cue in result.dropped:
                print(
                    f"warning: dropped zero-duration cue {cue.src} "
                    f"at {format_time(cue.start)}",
                    file=sys.stderr,
                )
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(format_srt(result.cues))
            print(
                f"merged {sum(len(c) for c in all_cues)} -> {len(result.cues)} cues, "
                f"dropped {len(result.dropped)}",
                file=sys.stderr,
            )
            return 0

        with open(args.input, encoding="utf-8-sig") as fh:
            cues = parse_srt(fh.read(), src=args.input)
        if (args.delta is None) == (args.segments is None):
            p.error("shift needs exactly one of --delta / --segments")
        try:
            if args.delta is not None:
                shifted = shift_cues(cues, _parse_delta(args.delta))
            else:
                shifted = shift_by_segments(cues, _parse_segments(args.segments))
        except OutOfBoundsError as err:
            sys.stderr.write(_oob_report(err))
            return 2
        # A segmented shift may introduce overlaps across segments: re-merge.
        result = merge_cues(shifted)
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(format_srt(result.cues))
        print(f"wrote {len(result.cues)} cues -> {args.output}", file=sys.stderr)
        return 0
    except SubtitleError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

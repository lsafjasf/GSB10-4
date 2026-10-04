"""SRT 字幕的解析与输出（仅标准库）。"""

from __future__ import annotations

import re
from typing import List

from .core import Cue

_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})")


def _to_ms(h: str, m: str, s: str, ms: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def format_ms(ms: int) -> str:
    """毫秒 -> HH:MM:SS,mmm"""
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def parse_srt(text: str, tag: str = "") -> List[Cue]:
    """解析 SRT 文本。零时长 / 负时长条目直接报错（不允许进入后续流程）。"""
    cues: List[Cue] = []
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    for block in blocks:
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        if not lines:
            continue
        # 序号行可省略，找到时间轴行为准
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ti is None:
            raise ValueError(f"缺少时间轴行: {block!r}")
        m = _TIME_RE.search(lines[ti])
        if not m:
            raise ValueError(f"无法解析时间轴: {lines[ti]!r}")
        start = _to_ms(*m.group(1, 2, 3, 4))
        end = _to_ms(*m.group(5, 6, 7, 8))
        body = "\n".join(lines[ti + 1:])
        cues.append(Cue(start, end, body, tag))
    return cues


def render_srt(cues: List[Cue]) -> str:
    """输出标准 SRT 文本（序号从 1 重新编号）。"""
    parts = []
    for i, cue in enumerate(cues, 1):
        parts.append(
            f"{i}\n{format_ms(cue.start)} --> {format_ms(cue.end)}\n{cue.text}")
    return "\n\n".join(parts) + ("\n" if parts else "")

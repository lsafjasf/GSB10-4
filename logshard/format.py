"""记录与分片头的字节格式定义。

记录行（单行，禁止内嵌换行）::

    <ts_ms> <seq> <payload>\n

- ``ts_ms``   : 十进制毫秒时间戳（int，允许为负，按数值比较）
- ``seq``     : 十进制序号（int，写入方保证每条记录恰好消耗一个序号）
- ``payload`` : 任意字节，但不得包含 ``\\n``；允许包含空格、制表符、非 UTF-8 字节

分片头（每个分片文件的第一行，由 RotatingWriter 在创建分片时写入）::

    #LSHRD v1 offset=<N>\n

- ``offset`` 是该分片第一个字节在全局追加字节流中的绝对偏移。
- 分片之间不允许按文件名排序；唯一的物理顺序依据就是 offset。
"""

from __future__ import annotations

import re

MARKER_PREFIX = b"#LSHRD v1 offset="
MARKER_RE = re.compile(rb"^#LSHRD v1 offset=(\d+)\n")

#: 读取分片头时最多扫描的字节数（头行必然很短）
MARKER_SCAN_LIMIT = 128


class FormatError(ValueError):
    """分片头或记录格式非法。"""


def format_record(ts_ms: int, seq: int, payload: bytes) -> bytes:
    """把一条记录编码为字节行。payload 中的换行会被替换为空格。"""
    payload = payload.replace(b"\n", b" ")
    return str(ts_ms).encode("ascii") + b" " + str(seq).encode("ascii") + b" " + payload + b"\n"


def parse_record(line: bytes) -> tuple[int, int, bytes]:
    """解析一条完整记录行（必须含结尾 ``\\n``）。

    返回 ``(ts_ms, seq, payload)``。格式非法时抛 :class:`FormatError`。
    """
    if not line.endswith(b"\n"):
        raise FormatError("record line missing trailing newline")
    parts = line[:-1].split(b" ", 2)
    if len(parts) != 3:
        raise FormatError("record line must have exactly 3 fields: %r" % (line[:64],))
    try:
        ts_ms = int(parts[0])
        seq = int(parts[1])
    except ValueError as exc:
        raise FormatError("bad ts/seq in record line: %r" % (line[:64],)) from exc
    return ts_ms, seq, parts[2]


def format_marker(offset: int) -> bytes:
    """编码分片头行。"""
    if offset < 0:
        raise FormatError("negative shard offset")
    return MARKER_PREFIX + str(offset).encode("ascii") + b"\n"


def parse_marker(head: bytes) -> int:
    """从分片开头若干字节中解析 offset。找不到合法头时抛 :class:`FormatError`。"""
    match = MARKER_RE.match(head)
    if match is None:
        raise FormatError("missing or malformed shard header (expected '#LSHRD v1 offset=<N>')")
    return int(match.group(1))

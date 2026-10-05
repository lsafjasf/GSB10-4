"""顺序存储设备的块标记解析库（仅标准库）。

物理形态
========
卷是一条严格顺序的字节流，由若干条「记录」首尾相接组成：

    记录 := 8 字节定长记录头 + 载荷
    记录头 := <4sI  (4 字节类型标记 + 4 字节小端无符号整数载荷长度)
    记录类型:
        b"DATA"  数据块,   长度 = 载荷字节数
        b"EOFM"  文件结束标记 (file mark), 长度必须为 0
        b"EOVM"  卷结束标记 (volume mark), 长度必须为 0

合法卷必须以 EOVM 记录收尾；EOVM 之后的字节不属于本卷，解析时忽略。

确定性判定（关键）
==================
记录头只在「记录边界偏移」上被解释。记录边界完全由前一条记录头部中
声明的长度推导（偏移 + 8 + 长度），与载荷内容无关。因此哪怕载荷内部
原样出现 b"EOFM\\x00\\x00\\x00\\x00" 或 b"EOVM..." 这 8 个字节，
解析器也会整段跳过，绝不会把它当成标记——不存在任何「猜测/回溯」，
也不依赖缓存状态。任意 (镜像, 偏移) 的解析结果是纯函数，重复解析
结果逐字节一致。

截断
====
在看到 EOVM 之前流就结束，一律抛 TruncatedVolume，报告：
已读完整记录数、缺失字节数、最后一个完整块之后的边界偏移，
以及截断发生在记录头还是载荷阶段。绝不静默结束。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum
from typing import BinaryIO, Iterator, List, Optional

HEADER_SIZE = 8
_HEADER = struct.Struct("<4sI")

TAG_DATA = b"DATA"
TAG_EOF = b"EOFM"
TAG_EOV = b"EOVM"


class Kind(Enum):
    """记录类型：数据块 / 文件结束标记 / 卷结束标记。"""

    DATA = "DATA"
    EOF = "EOF"
    EOV = "EOV"


_TAG_TO_KIND = {TAG_DATA: Kind.DATA, TAG_EOF: Kind.EOF, TAG_EOV: Kind.EOV}


def encode_data(payload: bytes) -> bytes:
    """编码一个数据块。"""
    return _HEADER.pack(TAG_DATA, len(payload)) + payload


def encode_eof() -> bytes:
    """编码一个文件结束标记（8 字节，无载荷）。"""
    return _HEADER.pack(TAG_EOF, 0)


def encode_eov() -> bytes:
    """编码一个卷结束标记（8 字节，无载荷）。"""
    return _HEADER.pack(TAG_EOV, 0)


@dataclass(frozen=True)
class Event:
    """一次成功解析出的记录。"""

    index: int           # 从 0 开始的完整记录序号
    kind: Kind           # 记录类型
    offset: int          # 记录头在卷中的起始偏移
    length: int          # 载荷长度（标记为 0）
    end: int             # 记录结束偏移 = offset + 8 + length
    payload: Optional[bytes] = None


class FormatError(Exception):
    """非法记录头（未知标记类型，或标记带非零长度）。"""

    def __init__(self, offset: int, tag: bytes):
        self.offset = offset
        self.tag = bytes(tag)
        super().__init__(f"offset {offset}: 非法记录头 {self.tag!r}")


@dataclass(frozen=True)
class Truncation:
    """卷尾截断报告。"""

    blocks_read: int          # 截断前已读完的完整记录数
    missing_bytes: int        # 还差多少字节才能读完当前记录
    last_complete_end: int    # 最后一个完整记录的结束边界偏移
    phase: str                # 'header' 记录头不完整 / 'payload' 载荷不完整
    offset: int               # 不完整记录的起始偏移（== last_complete_end）

    def __str__(self) -> str:
        return (
            f"卷尾截断: 已读完整记录 {self.blocks_read} 条, "
            f"缺失 {self.missing_bytes} 字节 (位于 {self.phase} 阶段), "
            f"最后一个完整块边界 = {self.last_complete_end}, "
            f"不完整记录起始 = {self.offset}"
        )


class TruncatedVolume(Exception):
    """卷在 EOVM 之前结束（含空卷）。"""

    def __init__(self, report: Truncation):
        self.report = report
        super().__init__(str(report))


def _decode_header(header: bytes, offset: int) -> Kind:
    tag, length = _HEADER.unpack(header)
    kind = _TAG_TO_KIND.get(tag)
    if kind is None or (kind is not Kind.DATA and length != 0):
        raise FormatError(offset, tag)
    return kind


def parse_record_at(buf, offset: int, index: int,
                    want_payload: bool = False) -> Event:
    """纯函数：解析 buf 中 offset 处的一条记录。

    结果只取决于 (buf 的字节, offset)，不依赖任何缓存。
    """
    avail = len(buf) - offset
    if avail < HEADER_SIZE:
        raise TruncatedVolume(Truncation(
            blocks_read=index,
            missing_bytes=HEADER_SIZE - max(avail, 0),
            last_complete_end=offset,
            phase="header",
            offset=offset,
        ))
    tag, length = _HEADER.unpack_from(buf, offset)
    kind = _TAG_TO_KIND.get(tag)
    if kind is None or (kind is not Kind.DATA and length != 0):
        raise FormatError(offset, tag)
    end = offset + HEADER_SIZE + length
    if len(buf) < end:
        raise TruncatedVolume(Truncation(
            blocks_read=index,
            missing_bytes=end - len(buf),
            last_complete_end=offset,
            phase="payload",
            offset=offset,
        ))
    payload = None
    if want_payload and kind is Kind.DATA:
        payload = bytes(buf[offset + HEADER_SIZE:end])
    return Event(index, kind, offset, length, end, payload)


def scan(buf, want_payload: bool = False) -> List[Event]:
    """一次性解析整卷（测试/演示用；大卷请用 iter_events）。

    空卷或缺少 EOVM 均抛 TruncatedVolume，绝不静默返回。
    """
    events: List[Event] = []
    offset = 0
    while True:
        event = parse_record_at(buf, offset, len(events), want_payload)
        events.append(event)
        offset = event.end
        if event.kind is Kind.EOV:
            return events


def _read_exact(f: BinaryIO, size: int) -> bytes:
    """读满 size 字节；底层 read 返回分片（记录跨块边界）也能拼全。"""
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = f.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def iter_events(f: BinaryIO, want_payload: bool = False,
                chunk_size: int = 1 << 18) -> Iterator[Event]:
    """流式顺序解析，逐记录产出 Event。

    内存占用 O(chunk_size)（默认 256 KiB），与卷总长度无关：
    记录头只读 8 字节，数据块载荷分块读出后即丢弃，不缓存任何历史。
    want_payload=True 时为保留当前块载荷，峰值 O(单块大小)。
    记录头跨越底层读块边界时由 _read_exact 保证拼全。
    """
    index = 0
    offset = 0
    while True:
        header = _read_exact(f, HEADER_SIZE)
        if len(header) < HEADER_SIZE:
            raise TruncatedVolume(Truncation(
                blocks_read=index,
                missing_bytes=HEADER_SIZE - len(header),
                last_complete_end=offset,
                phase="header",
                offset=offset,
            ))
        tag, length = _HEADER.unpack(header)
        kind = _TAG_TO_KIND.get(tag)
        if kind is None or (kind is not Kind.DATA and length != 0):
            raise FormatError(offset, tag)

        payload = None
        remaining = length
        if kind is Kind.DATA:
            if want_payload:
                payload = _read_exact(f, length)
                if len(payload) < length:
                    raise TruncatedVolume(Truncation(
                        blocks_read=index,
                        missing_bytes=length - len(payload),
                        last_complete_end=offset,
                        phase="payload",
                        offset=offset,
                    ))
                remaining = 0
            while remaining > 0:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            if remaining:
                raise TruncatedVolume(Truncation(
                    blocks_read=index,
                    missing_bytes=remaining,
                    last_complete_end=offset,
                    phase="payload",
                    offset=offset,
                ))

        end = offset + HEADER_SIZE + length
        yield Event(index, kind, offset, length, end, payload)
        index += 1
        offset = end
        if kind is Kind.EOV:
            return


# ---------------------------------------------------------------------------
# 顺序读写头：严格顺序推进，支持前进/后退各一步
# ---------------------------------------------------------------------------

class TapeReader:
    """在 bytes-like 镜像（bytes / mmap 等）上的顺序读写头。

    读写头位置 self.position 始终是一个记录边界偏移。

    状态序列（每一步之后报告）：
        BOT           位于卷首（0 号记录之前）
        AFTER_DATA    上一条是数据块
        AFTER_FILEMARK 上一条是文件结束标记
        AFTER_EOV     上一条是卷结束标记（卷已读完）

    位置与状态都不由缓存保存，而是每次从偏移 0 按长度纯重扫推导，
    因此前进、后退、再前进的结果与首次解析完全一致。
    """

    _STATE = {
        Kind.DATA: "AFTER_DATA",
        Kind.EOF: "AFTER_FILEMARK",
        Kind.EOV: "AFTER_EOV",
    }

    def __init__(self, data):
        self._data = data
        self._pos = 0

    @property
    def position(self) -> int:
        return self._pos

    def _records_before(self, limit: int) -> List[Event]:
        """从偏移 0 确定性重扫，返回完全落在 [0, limit) 内的记录。"""
        events: List[Event] = []
        offset = 0
        while offset < limit:
            event = parse_record_at(self._data, offset, len(events))
            if event.end > limit:
                break
            events.append(event)
            offset = event.end
            if event.kind is Kind.EOV:
                break
        return events

    def state(self) -> str:
        """当前读写头状态（纯函数，无缓存）。"""
        if self._pos == 0:
            return "BOT"
        events = self._records_before(self._pos)
        if not events:
            return "BOT"
        return self._STATE[events[-1].kind]

    def step_forward(self) -> Optional[Event]:
        """前进一步：解析并跨过当前位置的记录。

        已在 EOVM 之后返回 None；未到 EOVM 而流结束则抛
        TruncatedVolume（不静默结束）。
        """
        previous = self._records_before(self._pos)
        if previous and previous[-1].kind is Kind.EOV:
            return None
        event = parse_record_at(self._data, self._pos, len(previous))
        self._pos = event.end
        return event

    def step_back(self) -> Optional[Event]:
        """后退一步：读写头退到上一条记录之前，返回该记录。

        已在卷首返回 None。
        """
        if self._pos == 0:
            return None
        previous = self._records_before(self._pos)
        if not previous:
            return None
        target = previous[-1]
        self._pos = target.offset
        return target

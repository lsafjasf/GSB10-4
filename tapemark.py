"""tapemark - 顺序存储卷(块标记格式)的顺序读取库.

格式设计(全部大端, 记录紧密排列, 无填充无对齐):

    record        := header [payload payload_crc] trailer
    header (16B)  := MAGIC(4) type(1) reserved(3)=0 payload_len(4) header_crc32(4)
                     header_crc32 = crc32(header 前 12 字节)
    DATA 记录     := header + payload[payload_len] + crc32(payload)(4) + trailer(4)
    MARK 记录     := header(payload_len=0) + trailer(4)            # 总长恒为 20
    trailer (4B)  := total_len = 本记录总字节数(含 header 与 trailer)

    type: 0=DATA  1=FILEMARK(文件结束)  2=VOLUMEMARK(卷结束)
    volume := record*  且必须以一条 VOLUMEMARK 记录收尾.

标记判定依据(确定性):
    解析器从不"搜索"标记字节序列. 每条记录的边界完全由偏移算术决定:
    在偏移 p 处读 16 字节头, 校验 MAGIC 与头 CRC; 由 type/payload_len
    算出下一条记录的起始偏移 p + total_len, 直接跳过数据区.
    因此数据区里出现与标记完全相同的字节序列(哪怕是另一条合法记录
    的完整字节)也不会被误判——解析器根本不会去检查数据区的内容.
    判定结果只是 (镜像字节, 偏移) 的纯函数, 不依赖任何缓存或历史状态,
    同一镜像重复解析结果必然一致.

后退一步:
    每条记录尾部的 trailer 记录本记录总长. 位于偏移 p 时, 读 p-4 处
    的 trailer 得前一条记录总长 L, 前一条记录起于 p-L, 重新从镜像
    字节解析其头部即可, 无需保存任何历史栈, 内存 O(1).
"""

from __future__ import annotations

import os
import struct
import zlib
from typing import BinaryIO, Iterator, NamedTuple

MAGIC = b"TMB1"
HEADER_LEN = 16
TRAILER_LEN = 4
MARK_TOTAL_LEN = HEADER_LEN + TRAILER_LEN  # 标记记录总长, 恒为 20

TYPE_DATA = 0
TYPE_FILEMARK = 1
TYPE_VOLUMEMARK = 2
TYPE_NAMES = {TYPE_DATA: "DATA", TYPE_FILEMARK: "FILEMARK", TYPE_VOLUMEMARK: "VOLUMEMARK"}

STATE_READY = "READY"                # 下一条记录类型未知/为数据
STATE_AT_FILEMARK = "AT_FILEMARK"    # 刚越过一条 FILEMARK
STATE_END_OF_VOLUME = "END_OF_VOLUME"  # 已越过 VOLUMEMARK, 卷已读完

_HEADER = struct.Struct(">4sB3xII")   # magic, type, payload_len, header_crc32
_U32 = struct.Struct(">I")

DEFAULT_CHUNK = 64 * 1024             # 流式读数据区的块大小, 内存上界


# ---------------------------------------------------------------- 错误类型

class VolumeError(Exception):
    """卷解析错误的基类."""


class FormatError(VolumeError):
    """镜像字节与格式规则冲突(魔数/保留位/CRC/trailer 不符). 确定性报错."""

    def __init__(self, offset: int, reason: str):
        self.offset = offset
        self.reason = reason
        super().__init__(f"format error at offset {offset}: {reason}")


class TruncatedVolumeError(VolumeError):
    """卷尾截断: 在 offset 处开始的记录不完整(或卷缺少 VOLUMEMARK 收尾).

    属性:
        records_read      截断点之前已完整读出的记录数
        missing_bytes     补全当前这条记录至少还缺的字节数
        last_complete_end 最后一条完整记录的结束边界(=截断记录的起始偏移)
        phase             截断发生在记录的哪个部分: header/payload/payload_crc/trailer
    """

    def __init__(self, records_read: int, missing_bytes: int,
                 last_complete_end: int, phase: str):
        self.records_read = records_read
        self.missing_bytes = missing_bytes
        self.last_complete_end = last_complete_end
        self.phase = phase
        super().__init__(
            f"truncated volume: {records_read} complete record(s) read, "
            f"last complete record ends at offset {last_complete_end}, "
            f"next record cut in {phase}, at least {missing_bytes} byte(s) missing")


class EndOfVolume(VolumeError):
    """已越过 VOLUMEMARK 仍继续前进."""

    def __init__(self):
        super().__init__("already at end of volume")


class BeginningOfVolume(VolumeError):
    """在卷首仍继续后退."""

    def __init__(self):
        super().__init__("already at beginning of volume")


# ---------------------------------------------------------------- 记录

class Record(NamedTuple):
    index: int          # 卷内序号, 从 0 开始
    offset: int         # 记录起始偏移
    type: int           # TYPE_DATA / TYPE_FILEMARK / TYPE_VOLUMEMARK
    payload_len: int    # 数据字节数(标记记录恒为 0)
    total_len: int      # 记录总字节数(含头尾)

    @property
    def end(self) -> int:
        return self.offset + self.total_len

    @property
    def type_name(self) -> str:
        return TYPE_NAMES[self.type]

    def __str__(self) -> str:
        if self.type == TYPE_DATA:
            return (f"#{self.index} DATA        @{self.offset} "
                    f"payload={self.payload_len} total={self.total_len} end={self.end}")
        return (f"#{self.index} {self.type_name:<11} @{self.offset} "
                f"total={self.total_len} end={self.end}")


# ---------------------------------------------------------------- 底层编解码

def _encode_header(rectype: int, payload_len: int) -> bytes:
    head12 = _HEADER.pack(MAGIC, rectype, payload_len, 0)[:12]
    return head12 + _U32.pack(zlib.crc32(head12))


def _decode_header(buf: bytes, offset: int) -> "tuple[int, int]":
    """从 16 字节头解析 (type, payload_len), 不合法则抛 FormatError."""
    magic, rectype, payload_len, header_crc = _HEADER.unpack(buf)
    if magic != MAGIC:
        raise FormatError(offset, f"bad magic {magic!r}")
    if zlib.crc32(buf[:12]) != header_crc:
        raise FormatError(offset, "header crc32 mismatch")
    if rectype not in TYPE_NAMES:
        raise FormatError(offset, f"unknown record type {rectype}")
    if rectype != TYPE_DATA and payload_len != 0:
        raise FormatError(offset, "mark record with nonzero payload_len")
    return rectype, payload_len


def _record_total_len(rectype: int, payload_len: int) -> int:
    if rectype == TYPE_DATA:
        return HEADER_LEN + payload_len + 4 + TRAILER_LEN
    return MARK_TOTAL_LEN


# ---------------------------------------------------------------- 写入器

def write_data_block(f: BinaryIO, payload: bytes) -> int:
    """写入一条 DATA 记录, 返回记录总长."""
    total = _record_total_len(TYPE_DATA, len(payload))
    f.write(_encode_header(TYPE_DATA, len(payload)))
    f.write(payload)
    f.write(_U32.pack(zlib.crc32(payload)))
    f.write(_U32.pack(total))
    return total


def _write_mark(f: BinaryIO, rectype: int) -> int:
    f.write(_encode_header(rectype, 0))
    f.write(_U32.pack(MARK_TOTAL_LEN))
    return MARK_TOTAL_LEN


def write_filemark(f: BinaryIO) -> int:
    return _write_mark(f, TYPE_FILEMARK)


def write_volumemark(f: BinaryIO) -> int:
    return _write_mark(f, TYPE_VOLUMEMARK)


def build_volume(blocks=()) -> bytes:
    """按描述构造卷镜像. blocks 元素: bytes=数据块; "FILEMARK"=文件结束标记.

    末尾自动追加 VOLUMEMARK. 空 blocks 即空卷.
    """
    import io
    buf = io.BytesIO()
    for blk in blocks:
        if blk == "FILEMARK":
            write_filemark(buf)
        else:
            write_data_block(buf, bytes(blk))
    write_volumemark(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- 读取器

class Reader:
    """顺序读取器: 严格顺序推进, 支持前进/后退各一步.

    只保存 (文件对象, 当前偏移, 记录计数, 状态), 解析不依赖任何缓存;
    数据区按 chunk_size 流式读取做 CRC, 内存占用与卷长度无关.
    """

    def __init__(self, f: BinaryIO, chunk_size: int = DEFAULT_CHUNK):
        self._f = f
        self._chunk = max(4, chunk_size)
        self._size = os.fstat(f.fileno()).st_size
        self.position = 0            # 下一条记录的起始偏移
        self.records_read = 0        # 已越过的记录数
        self.state = STATE_READY

    # -- 底层: 定偏移读取, 短读即截断 --
    def _read_at(self, offset: int, n: int) -> bytes:
        self._f.seek(offset)
        return self._f.read(n)

    def _parse_header_at(self, offset: int) -> "tuple[int, int]":
        buf = self._read_at(offset, HEADER_LEN)
        if len(buf) < HEADER_LEN:
            raise TruncatedVolumeError(self.records_read, HEADER_LEN - len(buf),
                                       offset, "header")
        return _decode_header(buf, offset)

    def _check_payload_crc(self, offset: int, payload_len: int,
                           rec_offset: int) -> None:
        crc = 0
        remaining = payload_len
        pos = offset
        while remaining > 0:
            want = min(self._chunk, remaining)
            piece = self._read_at(pos, want)
            if len(piece) < want:
                raise TruncatedVolumeError(self.records_read,
                                           remaining - len(piece),
                                           rec_offset, "payload")
            crc = zlib.crc32(piece, crc)
            pos += want
            remaining -= want
        want = self._read_at(pos, 4)
        if len(want) < 4:
            raise TruncatedVolumeError(self.records_read, 4 - len(want),
                                       rec_offset, "payload_crc")
        if _U32.unpack(want)[0] != crc:
            raise FormatError(rec_offset, "payload crc32 mismatch")

    def _parse_record_at(self, offset: int, index: int) -> Record:
        """在 offset 处完整解析一条记录; 截断/格式错误均抛异常(确定性)."""
        rectype, payload_len = self._parse_header_at(offset)
        total = _record_total_len(rectype, payload_len)
        if rectype == TYPE_DATA:
            self._check_payload_crc(offset + HEADER_LEN, payload_len, offset)
        trailer_off = offset + total - TRAILER_LEN
        trailer = self._read_at(trailer_off, TRAILER_LEN)
        if len(trailer) < TRAILER_LEN:
            raise TruncatedVolumeError(self.records_read, TRAILER_LEN - len(trailer),
                                       offset, "trailer")
        if _U32.unpack(trailer)[0] != total:
            raise FormatError(offset, "trailer total_len mismatch")
        return Record(index, offset, rectype, payload_len, total)

    # -- 前进/后退 --
    def step_forward(self) -> Record:
        """前进一条记录, 返回该记录; 之后 position/state 已更新."""
        if self.state == STATE_END_OF_VOLUME:
            raise EndOfVolume()
        rec = self._parse_record_at(self.position, self.records_read)
        self.position = rec.end
        self.records_read += 1
        if rec.type == TYPE_VOLUMEMARK:
            self.state = STATE_END_OF_VOLUME
        elif rec.type == TYPE_FILEMARK:
            self.state = STATE_AT_FILEMARK
        else:
            self.state = STATE_READY
        return rec

    def step_back(self) -> Record:
        """后退一条记录, 返回被退过的那条记录; 之后 position/state 已更新.

        依据: 当前偏移 p 之前 4 字节是前一条记录的 trailer(其总长 L),
        前一条记录起于 p-L; 头部从镜像字节重新解析, 不用任何历史缓存.
        """
        if self.position == 0:
            raise BeginningOfVolume()
        trailer = self._read_at(self.position - TRAILER_LEN, TRAILER_LEN)
        if len(trailer) < TRAILER_LEN:  # 理论上只在镜像被外部截短时发生
            raise TruncatedVolumeError(self.records_read, TRAILER_LEN - len(trailer),
                                       self.position - TRAILER_LEN, "trailer")
        prev_total = _U32.unpack(trailer)[0]
        prev_off = self.position - prev_total
        if prev_off < 0:
            raise FormatError(self.position - TRAILER_LEN,
                              "trailer points before volume start")
        rec = self._parse_record_at(prev_off, self.records_read - 1)
        if rec.end != self.position:
            raise FormatError(prev_off, "record does not end where trailer claims")
        self.position = prev_off
        self.records_read -= 1
        self.state = STATE_READY
        return rec

    def scan(self) -> Iterator[Record]:
        """从头顺序解析到 VOLUMEMARK, 逐条产出记录(流式, O(1) 内存)."""
        while True:
            rec = self.step_forward()
            yield rec
            if rec.type == TYPE_VOLUMEMARK:
                return


# ---------------------------------------------------------------- CLI

def _main(argv: "list[str]") -> int:
    """python3 tapemark.py scan <image>  -- 顺序打印卷内记录与截断报告."""
    if len(argv) == 3 and argv[1] == "scan":
        with open(argv[2], "rb") as f:
            reader = Reader(f)
            try:
                for rec in reader.scan():
                    print(rec)
            except VolumeError as e:
                print(f"{type(e).__name__}: {e}")
                if isinstance(e, TruncatedVolumeError):
                    print(f"records_read={e.records_read} "
                          f"missing_bytes={e.missing_bytes} "
                          f"last_complete_end={e.last_complete_end} "
                          f"phase={e.phase}")
                return 2
            print(f"position={reader.position} state={reader.state}")
        return 0
    print("usage: python3 tapemark.py scan <image>")
    return 64


if __name__ == "__main__":
    raise SystemExit(_main(__import__("sys").argv))

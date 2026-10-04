"""appendlog — 崩溃安全的追加式记录日志（Python 3，仅标准库）。

记录格式（小端）：
    magic        4B   固定 b"RLG1"
    payload_len  4B   uint32
    header_crc   4B   crc32(magic || payload_len)
    payload      NB   业务数据
    payload_crc  4B   crc32(payload)

设计要点：
- 头部自带 crc32，长度字段被篡改会先被头部校验拦截；
- 载荷 crc32 可 100% 检出所有单比特翻转与所有 <=32bit 的突发错误
  （单字节翻转属于 <=8bit 突发，必然被检出）；
- scan() 全量校验并在坏记录后重同步，能报告文件中间的坏记录；
- locate_tail() 只读尾部窗口（>= 最大记录尺寸）即可定位最后一条
  完整记录，不需要全量扫描；
- recover() 在第一个不可恢复的缺陷处截断，之后可安全继续追加，
  追加结果与进程从未中断时逐字节一致。
"""

import os
import struct
import zlib

MAGIC = b"RLG1"
HEADER_SIZE = 12                 # magic(4) + payload_len(4) + header_crc(4)
CRC_SIZE = 4
OVERHEAD = HEADER_SIZE + CRC_SIZE
MAX_PAYLOAD_LIMIT = (1 << 32) - 1
DEFAULT_MAX_PAYLOAD = 64 * 1024 * 1024

_HDR = struct.Struct("<4sII")
_U32 = struct.Struct("<I")


# ---------------------------------------------------------------- 编码 / 写入

def encode_record(payload):
    """把一段 payload 编码为一条完整记录的字节串。"""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    n = len(payload)
    if n > MAX_PAYLOAD_LIMIT:
        raise ValueError("payload too large: %d" % n)
    hdr_wo_crc = MAGIC + _U32.pack(n)
    header = hdr_wo_crc + _U32.pack(zlib.crc32(hdr_wo_crc))
    return header + payload + _U32.pack(zlib.crc32(payload))


class AppendWriter:
    """追加写入器。每条记录一次 write；sync=True 时每条都 flush+fsync。"""

    def __init__(self, path):
        self.path = path
        self._f = open(path, "ab")

    def append(self, payload, sync=True):
        self._f.write(encode_record(payload))
        if sync:
            self.sync()

    def sync(self):
        self._f.flush()
        os.fsync(self._f.fileno())

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------- 数据结构

class Defect:
    """一处损坏：offset 为尝试解析记录的起始偏移。"""
    __slots__ = ("offset", "kind", "detail")

    def __init__(self, offset, kind, detail=""):
        self.offset = offset
        self.kind = kind
        self.detail = detail

    def __repr__(self):
        return "Defect(offset=%d, kind=%s, detail=%r)" % (
            self.offset, self.kind, self.detail)


class Record:
    """一条校验通过的完整记录。crc32 字段为记录内存储值，解析时已重算比对。"""
    __slots__ = ("offset", "payload_len", "total_len", "crc32", "payload")

    def __init__(self, offset, payload_len, crc32, payload=None):
        self.offset = offset
        self.payload_len = payload_len
        self.total_len = OVERHEAD + payload_len
        self.crc32 = crc32
        self.payload = payload

    @property
    def end(self):
        return self.offset + self.total_len

    def __repr__(self):
        return "Record(offset=%d, payload_len=%d, crc32=0x%08x)" % (
            self.offset, self.payload_len, self.crc32)


class ScanResult:
    def __init__(self, size, records, defects):
        self.size = size
        self.records = records
        self.defects = defects

    @property
    def clean(self):
        return not self.defects

    @property
    def boundary(self):
        """安全追加点：第一个缺陷的偏移；无缺陷时为文件末尾。"""
        return self.defects[0].offset if self.defects else self.size

    @property
    def last_record(self):
        return self.records[-1] if self.records else None


class RecoveryReport:
    def __init__(self, path, size_before, action, cut_offset, defects,
                 discarded_records, last_good_record):
        self.path = path
        self.size_before = size_before
        self.action = action                    # "clean" | "truncated"
        self.cut_offset = cut_offset
        self.defects = defects
        self.discarded_records = discarded_records
        self.last_good_record = last_good_record
        self.size_after = cut_offset


class TailInfo:
    def __init__(self, found, tail_offset, trailing_bytes, record, bytes_read):
        self.found = found
        self.tail_offset = tail_offset          # 最后一条完整记录的结束偏移
        self.trailing_bytes = trailing_bytes    # 其后的半条记录字节数
        self.record = record                    # 最后一条完整记录（可能为 None）
        self.bytes_read = bytes_read            # 实际从磁盘读取的字节数


# ---------------------------------------------------------------- 解析

def _read_record_at(f, offset, size, max_payload, want_payload=False):
    """在 offset 处解析一条记录，返回 (record, defect)，二者必居其一。"""
    remaining = size - offset
    if remaining < HEADER_SIZE:
        return None, Defect(offset, "truncated_header",
                            "header needs %dB, only %dB left" % (HEADER_SIZE, remaining))
    f.seek(offset)
    hdr = f.read(HEADER_SIZE)
    magic, n, hcrc = _HDR.unpack(hdr)
    if magic != MAGIC:
        return None, Defect(offset, "bad_magic", "magic=%r" % (hdr[:4],))
    if zlib.crc32(hdr[:8]) != hcrc:
        return None, Defect(offset, "bad_header_crc",
                            "stored=0x%08x computed=0x%08x" % (hcrc, zlib.crc32(hdr[:8])))
    if n > max_payload:
        return None, Defect(offset, "bad_length",
                            "payload_len=%d exceeds max_payload=%d" % (n, max_payload))
    total = OVERHEAD + n
    if remaining < total:
        have = remaining - HEADER_SIZE
        kind = "truncated_payload" if have < n else "truncated_crc"
        return None, Defect(offset, kind,
                            "payload_len=%d but only %dB available after header"
                            % (n, have))
    payload = f.read(n)
    (stored_crc,) = _U32.unpack(f.read(CRC_SIZE))
    computed = zlib.crc32(payload)
    if computed != stored_crc:
        return None, Defect(offset, "payload_crc_mismatch",
                            "stored=0x%08x computed=0x%08x" % (stored_crc, computed))
    return Record(offset, n, stored_crc, payload if want_payload else None), None


def _parse_record_buf(buf, pos, max_payload):
    """在内存缓冲 buf 的 pos 处尝试解析记录，成功返回 (total_len, payload_len, crc32)。"""
    if pos + HEADER_SIZE > len(buf):
        return None
    magic, n, hcrc = _HDR.unpack_from(buf, pos)
    if magic != MAGIC:
        return None
    if zlib.crc32(buf[pos:pos + 8]) != hcrc:
        return None
    if n > max_payload:
        return None
    end = pos + OVERHEAD + n
    if end > len(buf):
        return None
    (stored,) = _U32.unpack_from(buf, end - CRC_SIZE)
    if zlib.crc32(buf[pos + HEADER_SIZE:pos + HEADER_SIZE + n]) != stored:
        return None
    return OVERHEAD + n, n, stored


def _find_next_magic(f, start, size):
    """从 start 起流式查找下一个 MAGIC 的偏移，用于坏记录后的重同步。"""
    pos = start
    tail = b""
    f.seek(pos)
    while pos < size:
        chunk = f.read(min(1 << 20, size - pos))
        if not chunk:
            return None
        data = tail + chunk
        idx = data.find(MAGIC)
        if idx >= 0:
            return pos - len(tail) + idx
        tail = data[-(len(MAGIC) - 1):]
        pos += len(chunk)
    return None


# ---------------------------------------------------------------- 读取 / 校验

def scan(path, max_payload=DEFAULT_MAX_PAYLOAD, resync=True):
    """全量校验。返回 ScanResult。

    resync=True 时，遇到坏记录会继续向后查找下一条记录的 magic，
    因此文件中间的坏记录和其后仍然完好的记录都会被报告。
    """
    size = os.path.getsize(path)
    records, defects = [], []
    with open(path, "rb") as f:
        offset = 0
        while offset < size:
            rec, defect = _read_record_at(f, offset, size, max_payload)
            if defect is None:
                records.append(rec)
                offset = rec.end
                continue
            defects.append(defect)
            if not resync:
                break
            nxt = _find_next_magic(f, offset + 1, size)
            if nxt is None:
                break
            offset = nxt
    return ScanResult(size, records, defects)


def iter_records(path, max_payload=DEFAULT_MAX_PAYLOAD):
    """安全读取：逐条产出 (offset, payload)，遇到半条/坏记录即停止。

    调用方可配合 scan() 的 boundary 获得精确边界与缺陷原因。
    """
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        offset = 0
        while offset < size:
            rec, defect = _read_record_at(f, offset, size, max_payload,
                                          want_payload=True)
            if defect is not None:
                return
            yield rec.offset, rec.payload
            offset = rec.end


def locate_tail(path, max_record_size):
    """快速定位最后一条完整记录，只读尾部窗口，不做全量扫描。

    max_record_size 必须 >= 文件中最大单条记录的总尺寸（含 16B 开销）。
    尾部可能残留一条几乎写满的半记录（最长 max_record_size-1 字节），
    因此读取窗口取 2*max_record_size，保证最后一条完整记录一定完整
    落在窗口内。返回 TailInfo；窗口内找不到完整记录时 found=False
    （调用方应退化为 scan() 全量校验）。
    """
    size = os.path.getsize(path)
    if size == 0:
        return TailInfo(True, 0, 0, None, 0)
    window = min(size, 2 * max_record_size)
    base = size - window
    with open(path, "rb") as f:
        f.seek(base)
        buf = f.read(window)
    max_payload = max_record_size - OVERHEAD
    end_search = len(buf)
    while True:
        pos = buf.rfind(MAGIC, 0, end_search)
        if pos < 0:
            return TailInfo(False, None, None, None, window)
        parsed = _parse_record_buf(buf, pos, max_payload)
        if parsed is not None:
            total_len, payload_len, crc = parsed
            rec = Record(base + pos, payload_len, crc)
            return TailInfo(True, rec.end, size - rec.end, rec, window)
        end_search = pos


# ---------------------------------------------------------------- 恢复

def recover(path, max_payload=DEFAULT_MAX_PAYLOAD):
    """恢复到最近一致状态：在第一个不可恢复缺陷处截断。

    截断点 = 第一个缺陷的偏移 = 其前最后一条完整记录的结束位置。
    缺陷之后重同步到的完好记录无法原地保留（追加式文件不能挖洞），
    会一并丢弃并在报告中列出。返回 RecoveryReport。
    """
    res = scan(path, max_payload=max_payload, resync=True)
    if res.clean:
        return RecoveryReport(path, res.size, "clean", res.size, [], [],
                              res.last_record)
    cut = res.defects[0].offset
    discarded = [r for r in res.records if r.offset >= cut]
    last_good = None
    for r in res.records:
        if r.offset < cut:
            last_good = r
        else:
            break
    with open(path, "r+b") as f:
        f.truncate(cut)
    return RecoveryReport(path, res.size, "truncated", cut, res.defects,
                          discarded, last_good)

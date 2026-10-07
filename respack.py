"""respack — 资源包索引解析与局部替换(纯标准库)。

二进制格式 (小端):
    头部 HEADER (20 字节):
        magic       4s   = b"RPK1"
        version     u32  = 1
        entry_count u32
        data_offset u64  数据区起始(绝对偏移), 必须 >= 索引区末尾
    索引区 (entry_count 条变长记录, 紧跟头部):
        name_len    u16
        name        name_len 字节 (UTF-8)
        offset      u64  条目数据绝对偏移
        length      u64  条目数据长度
        crc32       u32  条目数据 CRC32
    数据区: 各条目原始字节, 区间由索引给出。

索引重算规则 (替换后):
    - 索引区总大小不变(记录布局与名字不变), 只原地重写目标条目的记录;
    - 追加型: 目标条目 offset := 替换前文件大小, length := 新数据长度,
      其余所有条目的 offset/length 逐字节不变, 文件大小 += 新数据长度;
    - 原地型: 要求 新长度 <= 旧长度; 所有条目 offset 不变,
      目标条目 length := 新数据长度, 文件大小不变。
"""
from __future__ import annotations

import os
import struct
import time
import zlib
from dataclasses import dataclass

MAGIC = b"RPK1"
VERSION = 1
HEADER = struct.Struct("<4sIIQ")          # magic, version, entry_count, data_offset
_U16 = struct.Struct("<H")
_U64 = struct.Struct("<Q")
_U32 = struct.Struct("<I")
REC_FIXED = 2 + 8 + 8 + 4                 # name_len + offset + length + crc32
COPY_CHUNK = 1 << 20


class PackError(Exception):
    """资源包错误。entry 属性指出出问题的条目名(若可定位)。"""

    def __init__(self, message: str, entry: str | None = None):
        super().__init__(message)
        self.entry = entry


@dataclass
class Entry:
    name: str
    offset: int
    length: int
    crc32: int
    rec_pos: int = 0      # 该条目索引记录在文件中的起始偏移
    rec_len: int = 0      # 该条目索引记录的字节长度


@dataclass
class Index:
    data_offset: int
    entries: list  # list[Entry], 索引顺序

    def by_name(self, name: str) -> Entry:
        for e in self.entries:
            if e.name == name:
                return e
        raise PackError(f"条目不存在: {name!r}", entry=name)


@dataclass
class ReplaceReport:
    strategy: str
    entry: str
    old_offset: int
    old_length: int
    new_offset: int
    new_length: int
    data_bytes_written: int
    index_bytes_written: int
    elapsed_sec: float

    @property
    def bytes_written(self) -> int:
        return self.data_bytes_written + self.index_bytes_written


def _crc32_of(path, offset, length) -> int:
    crc = 0
    remaining = length
    with open(path, "rb") as f:
        f.seek(offset)
        while remaining > 0:
            chunk = f.read(min(COPY_CHUNK, remaining))
            if not chunk:
                raise PackError("读取条目数据时遇到文件末尾")
            crc = zlib.crc32(chunk, crc)
            remaining -= len(chunk)
    return crc & 0xFFFFFFFF


def read_index(path: str, verify_crc: bool = False) -> Index:
    """解析并校验索引。损坏/越界/重叠时抛 PackError 并指明条目。"""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        raw = f.read(HEADER.size)
        if len(raw) < HEADER.size:
            raise PackError("索引损坏: 文件小于头部长度")
        magic, version, count, data_offset = HEADER.unpack(raw)
        if magic != MAGIC:
            raise PackError(f"索引损坏: magic 不匹配 ({magic!r})")
        if version != VERSION:
            raise PackError(f"索引损坏: 不支持的版本 {version}")

        entries = []
        pos = HEADER.size
        for i in range(count):
            rec_pos = pos
            raw_len = f.read(2)
            if len(raw_len) < 2:
                raise PackError(f"索引损坏: 第 {i} 条记录被截断")
            name_len = _U16.unpack(raw_len)[0]
            name_b = f.read(name_len)
            if len(name_b) < name_len:
                raise PackError(f"索引损坏: 第 {i} 条记录的名字被截断")
            try:
                name = name_b.decode("utf-8")
            except UnicodeDecodeError:
                raise PackError(f"索引损坏: 第 {i} 条记录的名字不是合法 UTF-8")
            fixed = f.read(8 + 8 + 4)
            if len(fixed) < 20:
                raise PackError(f"索引损坏: 第 {i} 条记录被截断")
            offset = _U64.unpack_from(fixed, 0)[0]
            length = _U64.unpack_from(fixed, 8)[0]
            crc = _U32.unpack_from(fixed, 16)[0]
            pos = rec_pos + REC_FIXED + name_len
            entries.append(Entry(name, offset, length, crc,
                                 rec_pos=rec_pos, rec_len=REC_FIXED + name_len))
        index_end = pos

    if data_offset < index_end:
        raise PackError(f"索引损坏: data_offset({data_offset}) 小于索引区末尾({index_end})")

    for e in entries:
        if e.offset < data_offset:
            raise PackError(
                f"条目 {e.name!r} 越界: offset({e.offset}) 位于数据区起点({data_offset})之前",
                entry=e.name)
        if e.offset + e.length > size:
            raise PackError(
                f"条目 {e.name!r} 长度越界: offset+length={e.offset + e.length} "
                f"超出文件大小 {size}", entry=e.name)

    ordered = sorted(entries, key=lambda e: e.offset)
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.offset < prev.offset + prev.length:
            raise PackError(
                f"条目区间重叠: {prev.name!r} [{prev.offset},{prev.offset + prev.length}) 与 "
                f"{cur.name!r} [{cur.offset},{cur.offset + cur.length})",
                entry=cur.name)

    if verify_crc:
        for e in entries:
            actual = _crc32_of(path, e.offset, e.length)
            if actual != e.crc32:
                raise PackError(
                    f"条目 {e.name!r} CRC 校验失败: 索引={e.crc32:#010x} 实际={actual:#010x}",
                    entry=e.name)
    return Index(data_offset=data_offset, entries=entries)


def _encode_record(e: Entry) -> bytes:
    name_b = e.name.encode("utf-8")
    return _U16.pack(len(name_b)) + name_b + _U64.pack(e.offset) + _U64.pack(e.length) + _U32.pack(e.crc32)


def build_pack(path: str, entries) -> None:
    """根据 [(name, data)] 构建资源包。data 为 bytes, 或 (length, chunk_iter_factory)。"""
    norm = []
    for name, data in entries:
        if isinstance(data, tuple):
            length, factory = data
        else:
            length, factory = len(data), lambda d=data: iter([d])
        norm.append((name, length, factory))

    index_size = sum(REC_FIXED + len(n.encode("utf-8")) for n, _, _ in norm)
    data_offset = HEADER.size + index_size

    pos = data_offset
    recs = []
    rec_pos = HEADER.size
    for name, length, _ in norm:
        recs.append(Entry(name, pos, length, 0, rec_pos=rec_pos,
                          rec_len=REC_FIXED + len(name.encode("utf-8"))))
        pos += length
        rec_pos += REC_FIXED + len(name.encode("utf-8"))

    with open(path, "wb") as f:
        f.write(HEADER.pack(MAGIC, VERSION, len(norm), data_offset))
        for e in recs:
            f.write(_encode_record(e))  # crc 先写 0, 数据写完后回填
        for e, (_, _, factory) in zip(recs, norm):
            crc = 0
            for chunk in factory():
                crc = zlib.crc32(chunk, crc)
                f.write(chunk)
            e.crc32 = crc & 0xFFFFFFFF
            f.seek(e.rec_pos + e.rec_len - 4)
            f.write(_U32.pack(e.crc32))
            f.seek(0, os.SEEK_END)


def replace(path: str, name: str, new_data: bytes, strategy: str = "append") -> ReplaceReport:
    """局部替换一个条目。strategy: "append" | "inplace"。"""
    t0 = time.perf_counter()
    idx = read_index(path)
    target = idx.by_name(name)
    old_length = target.length
    new_length = len(new_data)
    new_crc = zlib.crc32(new_data) & 0xFFFFFFFF
    data_written = 0

    with open(path, "r+b") as f:
        if strategy == "append":
            f.seek(0, os.SEEK_END)
            new_offset = f.tell()
            for i in range(0, new_length, COPY_CHUNK):
                f.write(new_data[i:i + COPY_CHUNK])
            data_written = new_length
        elif strategy == "inplace":
            if new_length > old_length:
                raise PackError(
                    f"原地型替换要求 新长度({new_length}) <= 旧长度({old_length}), 条目 {name!r}",
                    entry=name)
            new_offset = target.offset
            f.seek(new_offset)
            f.write(new_data)
            data_written = new_length
        else:
            raise PackError(f"未知替换策略: {strategy!r}")

        updated = Entry(name, new_offset, new_length, new_crc)
        rec = _encode_record(updated)
        f.seek(target.rec_pos)
        f.write(rec)
        index_written = len(rec)

    return ReplaceReport(
        strategy=strategy, entry=name,
        old_offset=target.offset, old_length=old_length,
        new_offset=new_offset, new_length=new_length,
        data_bytes_written=data_written, index_bytes_written=index_written,
        elapsed_sec=time.perf_counter() - t0)


def _exact_ranges(ba: bytes, bb: bytes, base: int):
    """两个字节串(等长)的精确差异区间, base 为它们在文件中的起始偏移。"""
    ranges = []
    start = prev = None
    for i in range(len(ba)):
        if ba[i] != bb[i]:
            if start is None:
                start = i
            elif i != prev + 1:
                ranges.append((base + start, base + prev + 1))
                start = i
            prev = i
    if start is not None:
        ranges.append((base + start, base + prev + 1))
    return ranges


def diff_ranges(path_a: str, path_b: str, page: int = 1 << 16,
                exact_limit: int = 32 << 20):
    """差异定位: 先按页(64KiB)比较, 差异游程内再做字节级精确扫描。

    单个游程超过 exact_limit(默认 32MiB)时退化为页级粒度(区间可能含未变字节)。
    返回 [(start, end), ...]; 文件长度不同时尾部多出部分记为一个区间。
    """
    size_a, size_b = os.path.getsize(path_a), os.path.getsize(path_b)
    limit = min(size_a, size_b)
    diff_pages = []
    n_pages = (limit + page - 1) // page
    with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
        for p in range(n_pages):
            if fa.read(page) != fb.read(page):
                diff_pages.append(p)

    runs = []
    for p in diff_pages:
        if runs and p == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], p)
        else:
            runs.append((p, p))

    ranges = []
    with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
        for first_p, last_p in runs:
            ps, pe = first_p * page, min((last_p + 1) * page, limit)
            if pe - ps > exact_limit:
                ranges.append((ps, pe))
                continue
            fa.seek(ps)
            fb.seek(ps)
            pos = ps
            while pos < pe:
                ba = fa.read(page)
                bb = fb.read(page)
                if ba != bb:
                    ranges.extend(_exact_ranges(ba, bb, pos))
                pos += len(ba)
    if size_a != size_b:
        ranges.append((limit, max(size_a, size_b)))
    merged = []
    for s, e in sorted(ranges):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def verify_others_unchanged(path_a: str, path_b: str, target_name: str) -> dict:
    """验证: 除目标条目外, 其余条目数据逐字节一致; 索引重算符合规则。

    返回差异定位数据: {"diff_ranges": [...], "regions": [...]}。
    """
    idx_a = read_index(path_a)
    idx_b = read_index(path_b)
    names_a = [e.name for e in idx_a.entries]
    names_b = [e.name for e in idx_b.entries]
    if names_a != names_b:
        raise PackError("替换前后条目集合/顺序不一致")

    ea = idx_a.by_name(target_name)
    eb = idx_b.by_name(target_name)

    for name in names_a:
        a, b = idx_a.by_name(name), idx_b.by_name(name)
        if name == target_name:
            continue
        if (a.offset, a.length, a.crc32) != (b.offset, b.length, b.crc32):
            raise PackError(f"非目标条目 {name!r} 的索引记录被改动", entry=name)
        with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
            fa.seek(a.offset)
            fb.seek(b.offset)
            remaining = a.length
            while remaining > 0:
                ca = fa.read(min(COPY_CHUNK, remaining))
                cb = fb.read(min(COPY_CHUNK, remaining))
                if ca != cb:
                    raise PackError(f"非目标条目 {name!r} 的数据字节发生变化", entry=name)
                remaining -= len(ca)

    # 差异定位: 每个差异区间必须落在 目标条目索引记录 / 目标旧数据 / 目标新数据 内
    ranges = diff_ranges(path_a, path_b)
    allowed = [
        (ea.rec_pos, ea.rec_pos + ea.rec_len, "index-record"),
        (ea.offset, ea.offset + ea.length, "target-old-data"),
        (eb.offset, eb.offset + eb.length, "target-new-data"),
    ]
    regions = []
    for s, e in ranges:
        hits = [label for ls, le, label in allowed if s >= ls and e <= le]
        if not hits:
            raise PackError(f"差异区间 [{s},{e}) 不在允许区域内")
        regions.append({"start": s, "end": e, "bytes": e - s, "region": hits[0]})
    return {"diff_ranges": ranges, "regions": regions,
            "entries_checked": len(names_a) - 1}

"""分片发现、缺口检测与流式合并。

两层顺序（详见 README）：

1. 物理重组层：分片按头里的 ``offset`` 升序拼接，offset 连续区间构成"段"，
   段与段之间是缺口（被手工删掉的分片）。绝不使用文件名排序。
2. 逻辑排序层：默认输出保持写入顺序（等价于 seq 升序），排序键为
   ``(ts_eff, seq, 物理偏移)``，其中 ``ts_eff = max(原始 ts, 上一条的 ts_eff)``
   是对回拨时钟的单调修复；修复行为会以事件形式上报。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator, Sequence

from .format import MARKER_SCAN_LIMIT, FormatError, parse_marker, parse_record

CHUNK_SIZE = 65536


@dataclass(frozen=True)
class ShardMeta:
    path: Path
    offset: int       # 分片头之后第一个数据字节在逻辑流中的绝对偏移
    size: int         # 文件总字节数（含头）
    marker_len: int   # 头行长度

    @property
    def data_len(self) -> int:
        return self.size - self.marker_len

    @property
    def data_end(self) -> int:
        return self.offset + self.data_len


@dataclass
class MergedRecord:
    ts_ms: int
    seq: int
    payload: bytes
    raw: bytes  # 含结尾 \n 的原始行
    start_offset: int
    end_offset: int
    ts_eff: int = 0  # 单调修复后的排序时间戳
    parse_ok: bool = True


@dataclass
class GapEvent:
    """一段缺失的字节区间 [gap_start, gap_end)。"""

    gap_start: int
    gap_end: int
    missing_bytes: int
    missing_shards_lb: int  # 至少缺失的分片数（按 max_bytes 推算的下界）
    prev_record: tuple[int, int] | None = None  # 缺口前最后一条完整记录 (ts, seq)
    next_record: tuple[int, int] | None = None  # 缺口后第一条完整记录 (ts, seq)
    ts_interval: tuple[int, int] | None = None  # 丢失记录时间戳落在的开区间
    lost_records_lb: int | None = None  # 丢失记录数下界；None 表示无法估计
    lb_degraded: bool = False  # True 表示因 seq 异常，下界只是启发式估计


@dataclass
class DanglingTailEvent:
    """缺口左侧残留的半条记录（记录前缀，后半已丢失）。"""

    offset: int
    length: int
    ts_ms: int | None
    seq: int | None
    preview: bytes


@dataclass
class DanglingHeadEvent:
    """缺口右侧残留的半条记录（记录后缀，前半已丢失）。"""

    offset: int
    length: int
    preview: bytes


@dataclass
class TruncatedTailEvent:
    """最后一个分片末尾的半条记录（轮转发生在写入中途，或文件被截断）。"""

    offset: int
    length: int
    preview: bytes


@dataclass
class RollbackEvent:
    seq: int
    ts_ms: int
    prev_ts_eff: int


@dataclass
class DuplicateSeqEvent:
    seq: int
    first_offset: int
    second_offset: int


@dataclass
class ParseErrorEvent:
    offset: int
    reason: str
    raw: bytes


@dataclass
class MergeReport:
    shards_total: int = 0
    shards_present: int = 0
    segments: int = 0
    records: int = 0
    gaps: list[GapEvent] = field(default_factory=list)
    dangling_tails: list[DanglingTailEvent] = field(default_factory=list)
    dangling_heads: list[DanglingHeadEvent] = field(default_factory=list)
    truncated_tails: list[TruncatedTailEvent] = field(default_factory=list)
    rollbacks: list[RollbackEvent] = field(default_factory=list)
    duplicate_seqs: list[DuplicateSeqEvent] = field(default_factory=list)
    parse_errors: list[ParseErrorEvent] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.gaps or self.truncated_tails or self.parse_errors)


def discover_shards(paths: Sequence[str | os.PathLike[str]]) -> list[ShardMeta]:
    """读取每个分片头部的 offset，按 offset 升序返回。

    - 头缺失/非法 -> FormatError（拒绝猜测顺序）
    - offset 重复或区间重叠 -> FormatError（数据损坏，拒绝静默继续）
    """
    metas: list[ShardMeta] = []
    for p in paths:
        path = Path(p)
        with open(path, "rb") as fh:
            head = fh.read(MARKER_SCAN_LIMIT)
        nl = head.find(b"\n")
        if nl < 0:
            raise FormatError(f"{path}: malformed shard header (no newline)")
        marker_len = nl + 1
        offset = parse_marker(head)
        metas.append(ShardMeta(path=path, offset=offset, size=path.stat().st_size, marker_len=marker_len))
    metas.sort(key=lambda m: m.offset)
    for prev, cur in zip(metas, metas[1:]):
        if cur.offset == prev.offset:
            raise FormatError(f"duplicate shard offset {cur.offset}: {prev.path} vs {cur.path}")
        if cur.offset < prev.data_end:
            raise FormatError(
                f"overlapping shards: {prev.path} [{prev.offset},{prev.data_end}) vs "
                f"{cur.path} [{cur.offset},{cur.data_end})"
            )
    return metas


def _parse_prefix(line: bytes) -> tuple[int | None, int | None]:
    """尽力从半行/整行里解析 (ts, seq)，失败返回 (None, None)。"""
    parts = line.split(b" ", 2)
    if len(parts) < 2:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None


class Merger:
    """流式合并器：常数内存，按物理顺序吐出重组后的完整记录。

    用法::

        merger = Merger(paths)
        for rec in merger.records():
            ...
        report = merger.report  # 迭代结束后可用
    """

    def __init__(
        self,
        paths: Sequence[str | os.PathLike[str]],
        max_shard_bytes: int | None = None,
        detect_duplicates: bool = True,
    ) -> None:
        self.shards = discover_shards(paths)
        self.max_shard_bytes = max_shard_bytes
        self.detect_duplicates = detect_duplicates
        self.report = MergeReport(shards_total=len(self.shards), shards_present=len(self.shards))

    # ---- 内部状态（records() 单次遍历期间有效）----

    def records(self) -> Iterator[MergedRecord]:
        report = self.report
        pending = bytearray()  # 当前正在拼接的行（可能跨分片）
        pending_start = 0
        stream_pos = 0  # 已消费字节在全局流中的偏移
        prev_rec: MergedRecord | None = None
        pending_gap: GapEvent | None = None
        expect_dangling_head = False
        ts_eff_prev: int | None = None
        last_seq: int | None = None
        # seq -> 首次出现的 offset。关闭 detect_duplicates 时为 None，内存 O(1)。
        seq_by_key: dict[int, int] | None = {} if self.detect_duplicates else None
        seq_anomaly = False

        def note_seq(seq: int, offset: int) -> None:
            nonlocal seq_anomaly
            if seq_by_key is None:
                return
            first = seq_by_key.get(seq)
            if first is not None:
                report.duplicate_seqs.append(DuplicateSeqEvent(seq=seq, first_offset=first, second_offset=offset))
                seq_anomaly = True
            else:
                seq_by_key[seq] = offset

        def finalize_gap(next_rec: MergedRecord | None) -> None:
            nonlocal pending_gap
            gap = pending_gap
            if gap is None:
                return
            if next_rec is not None and next_rec.parse_ok:
                gap.next_record = (next_rec.ts_ms, next_rec.seq)
                if gap.prev_record is not None:
                    prev_ts, prev_seq = gap.prev_record
                    gap.ts_interval = (prev_ts, next_rec.ts_ms)
                    gap.lost_records_lb = max(0, next_rec.seq - prev_seq - 1)
                    gap.lb_degraded = seq_anomaly
                else:
                    gap.lost_records_lb = next_rec.seq - 1 if next_rec.seq >= 1 else None
                    gap.lb_degraded = seq_anomaly
            report.gaps.append(gap)
            pending_gap = None

        def emit_line(line: bytes, start: int) -> MergedRecord:
            nonlocal ts_eff_prev, last_seq
            end = start + len(line)
            try:
                ts_ms, seq, payload = parse_record(line)
                rec = MergedRecord(ts_ms=ts_ms, seq=seq, payload=payload, raw=line, start_offset=start, end_offset=end)
            except FormatError as exc:
                rec = MergedRecord(
                    ts_ms=0, seq=-1, payload=line, raw=line, start_offset=start, end_offset=end, parse_ok=False
                )
                report.parse_errors.append(ParseErrorEvent(offset=start, reason=str(exc), raw=line))
            if rec.parse_ok:
                eff = rec.ts_ms if ts_eff_prev is None else max(rec.ts_ms, ts_eff_prev)
                if ts_eff_prev is not None and rec.ts_ms < ts_eff_prev:
                    report.rollbacks.append(RollbackEvent(seq=rec.seq, ts_ms=rec.ts_ms, prev_ts_eff=ts_eff_prev))
                rec.ts_eff = eff
                ts_eff_prev = eff
                note_seq(rec.seq, start)
                last_seq = rec.seq
            else:
                rec.ts_eff = ts_eff_prev if ts_eff_prev is not None else 0
            finalize_gap(rec)
            report.records += 1
            return rec

        for meta in self.shards:
            if meta.offset > stream_pos:
                # 分片缺失：进入缺口
                if pending:
                    ts_p, seq_p = _parse_prefix(bytes(pending))
                    report.dangling_tails.append(
                        DanglingTailEvent(
                            offset=pending_start, length=len(pending), ts_ms=ts_p, seq=seq_p, preview=bytes(pending[:64])
                        )
                    )
                    pending.clear()
                gap = GapEvent(
                    gap_start=stream_pos,
                    gap_end=meta.offset,
                    missing_bytes=meta.offset - stream_pos,
                    missing_shards_lb=-(-(meta.offset - stream_pos) // self.max_shard_bytes)
                    if self.max_shard_bytes
                    else 1,
                )
                if prev_rec is not None and prev_rec.parse_ok:
                    gap.prev_record = (prev_rec.ts_ms, prev_rec.seq)
                pending_gap = gap
                expect_dangling_head = True
                stream_pos = meta.offset
                report.segments += 1
            elif meta.offset == 0 or report.segments == 0:
                report.segments += 1

            with open(meta.path, "rb") as fh:
                first_line = True
                buf = b""
                while True:
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        nl = buf.find(b"\n")
                        if nl < 0:
                            break
                        line = buf[: nl + 1]
                        buf = buf[nl + 1 :]
                        if first_line:
                            first_line = False
                            continue  # 分片头不属于逻辑数据流，不推进 stream_pos
                        if expect_dangling_head:
                            # 缺口右侧的第一行一定是丢失记录的后缀，无法还原，丢弃并上报
                            report.dangling_heads.append(
                                DanglingHeadEvent(offset=stream_pos, length=len(line), preview=line[:64])
                            )
                            expect_dangling_head = False
                            stream_pos += len(line)
                            continue
                        if pending:
                            pending += line
                            full = bytes(pending)
                            pending.clear()
                        else:
                            pending_start = stream_pos
                            full = line
                        rec = emit_line(full, pending_start)
                        prev_rec = rec
                        stream_pos += len(line)
                        yield rec
                if buf:
                    # 行被切到下一个分片（或文件结尾）
                    if not pending:
                        pending_start = stream_pos
                    pending += buf
                    stream_pos += len(buf)

        if pending:
            ts_p, seq_p = _parse_prefix(bytes(pending))
            report.truncated_tails.append(
                TruncatedTailEvent(offset=pending_start, length=len(pending), preview=bytes(pending[:64]))
            )
            pending.clear()
        finalize_gap(None)

    def merged_bytes(self) -> Iterator[bytes]:
        """逐条产出重组后的原始记录行字节。"""
        for rec in self.records():
            yield rec.raw

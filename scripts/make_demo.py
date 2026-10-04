#!/usr/bin/env python3
"""生成对拍数据：轮转分片 -> 手工删若干分片 -> 独立 oracle -> 合并 -> 逐字节对比。

输出到 demo/：
  shards/           存活的分片（已删除 2 个，模拟手工清理）
  expected.merged   独立 oracle 计算的期望合并结果
  actual.merged     库实际合并输出（必须与 expected 逐字节一致）
  gap_report.json   缺口报告（时间区间 + 丢失记录数下界）
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from logshard import Merger, RotatingWriter, SeqLogger, format_record  # noqa: E402
from logshard.cli import _report_dict  # noqa: E402

DEMO = ROOT / "demo"
MAX_BYTES = 1024
DELETE_INDICES = {7, 23}  # 被"手工删掉"的分片序号（1-based 文件名序号）


def build_records() -> list[tuple[int, bytes]]:
    """确定性混合负载：正常递增、同毫秒突发、时钟回拨、长 payload。"""
    records: list[tuple[int, bytes]] = []
    ts = 1_700_000_000_000
    for i in range(400):
        ts += 3
        records.append((ts, f"normal msg {i:04d}".encode()))
    for j in range(30):  # 同毫秒 30 条
        records.append((ts, f"same-ms burst {j:02d}".encode()))
    for j in range(20):  # 时钟回拨段
        records.append((ts - 5000 + j * 10, f"rollback msg {j:02d}".encode()))
    records.append((ts + 1, b"long-payload " + b"L" * 500))  # 必跨分片的长记录
    for i in range(400, 800):
        ts += 3
        records.append((ts, f"normal msg {i:04d}".encode()))
    return records


def oracle_expected(records: list[tuple[int, bytes]], max_bytes: int, deleted: set[int]) -> bytes:
    """独立 oracle：模拟逻辑字节流与分片区间，只保留完整落在存活区间内的记录行。"""
    lines = [format_record(ts, seq, payload) for seq, (ts, payload) in enumerate(records, start=1)]
    stream = b"".join(lines)
    marker_len = len(b"#LSHRD v1 offset=") + len(str(len(stream))) + 1  # 头长上界，用于模拟
    # 模拟轮转切分（与 RotatingWriter 相同的纯字节规则，但独立实现）
    intervals: list[tuple[int, int]] = []  # 每个分片覆盖的逻辑区间 [start, end)
    pos, idx = 0, 1
    while pos < len(stream):
        marker = f"#LSHRD v1 offset={pos}\n".encode()
        room = max_bytes - len(marker)
        if room <= 0:
            raise RuntimeError("max_bytes too small for marker")
        end = min(len(stream), pos + room)
        if idx not in deleted:
            intervals.append((pos, end))
        pos = end
        idx += 1
    # 相邻存活区间合并成"段"（跨分片但在同一段内的记录应被保留）
    segments: list[list[int]] = []
    for a, b in intervals:
        if segments and segments[-1][1] == a:
            segments[-1][1] = b
        else:
            segments.append([a, b])
    out = bytearray()
    cursor = 0
    for line in lines:
        s, e = cursor, cursor + len(line)
        cursor = e
        if any(a <= s and e <= b for a, b in segments):
            out += line
    return bytes(out)


def main() -> int:
    if DEMO.exists():
        shutil.rmtree(DEMO)
    shards_dir = DEMO / "shards"
    shards_dir.mkdir(parents=True)

    records = build_records()
    with SeqLogger(RotatingWriter(shards_dir, max_bytes=MAX_BYTES)) as logger:
        for ts, payload in records:
            logger.log(ts, payload)

    all_shards = sorted(shards_dir.iterdir())
    for p in all_shards:
        idx = int(p.name.rsplit("-", 1)[1])
        if idx in DELETE_INDICES:
            p.unlink()
    present = sorted(shards_dir.iterdir())
    print(f"records={len(records)} shards_total={len(all_shards)} present={len(present)} deleted={sorted(DELETE_INDICES)}")

    expected = oracle_expected(records, MAX_BYTES, DELETE_INDICES)
    (DEMO / "expected.merged").write_bytes(expected)

    merger = Merger([str(p) for p in present], max_shard_bytes=MAX_BYTES)
    with open(DEMO / "actual.merged", "wb") as fh:
        for rec in merger.records():
            fh.write(rec.raw)
    report = merger.report
    (DEMO / "gap_report.json").write_text(
        json.dumps(_report_dict(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    actual = (DEMO / "actual.merged").read_bytes()
    ok = actual == expected
    print(f"oracle_match={ok} merged_records={report.records} gaps={len(report.gaps)}")
    for g in report.gaps:
        print(
            f"  gap bytes=[{g.gap_start},{g.gap_end}) ts_interval={g.ts_interval} "
            f"lost_records_lb={g.lost_records_lb} missing_shards_lb={g.missing_shards_lb}"
        )
    print(f"rollbacks={len(report.rollbacks)} dangling_tail={len(report.dangling_tails)} dangling_head={len(report.dangling_heads)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

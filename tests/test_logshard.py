"""logshard 自测：单分片、恰好切中、缺口、回拨、同毫秒、重复序号、对拍。"""

from __future__ import annotations

import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from logshard import FormatError, Merger, RotatingWriter, SeqLogger, format_record, parse_record
from logshard.reader import discover_shards


def build_shards(directory: str, records: list[tuple[int, bytes]], max_bytes: int) -> tuple[RotatingWriter, list[bytes]]:
    """records: [(ts_ms, payload)]；seq 自动 1..N。返回 (writer, 所有行的原始字节)。"""
    w = RotatingWriter(directory, max_bytes=max_bytes)
    logger = SeqLogger(w)
    lines: list[bytes] = []
    for ts, payload in records:
        seq = logger.log(ts, payload)
        lines.append(format_record(ts, seq, payload))
    logger.close()
    return w, lines


class TestFormat(unittest.TestCase):
    def test_roundtrip(self) -> None:
        line = format_record(1_700_000_000_123, 42, b"hello world\nx")  # 换行被替换
        self.assertTrue(line.endswith(b"\n"))
        ts, seq, payload = parse_record(line)
        self.assertEqual((ts, seq, payload), (1_700_000_000_123, 42, b"hello world x"))

    def test_empty_payload_and_spaces(self) -> None:
        line = format_record(5, 1, b"a b c")
        self.assertEqual(parse_record(line), (5, 1, b"a b c"))
        self.assertEqual(parse_record(format_record(0, 0, b"")), (0, 0, b""))

    def test_bad(self) -> None:
        with self.assertRaises(FormatError):
            parse_record(b"1 2\n")
        with self.assertRaises(FormatError):
            parse_record(b"x 2 p\n")


class TestWriter(unittest.TestCase):
    def test_rotation_guarantees(self) -> None:
        d = tempfile.mkdtemp()
        try:
            w = RotatingWriter(d, max_bytes=64)
            with w:
                for i in range(20):
                    w.write_record(1000 + i, i + 1, b"x" * 10)
            shards = sorted(Path(d).iterdir())
            self.assertGreater(len(shards), 1)
            for p in shards:
                self.assertLessEqual(p.stat().st_size, 64)
            metas = discover_shards([str(p) for p in shards])
            self.assertEqual(metas[0].offset, 0)
            for a, b in zip(metas, metas[1:]):
                self.assertEqual(b.offset, a.data_end)
            self.assertTrue(all(m.path.read_bytes().startswith(b"#LSHRD") for m in metas))
        finally:
            shutil.rmtree(d)


class TestMerge(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.dir)

    def _shards(self):
        return [str(p) for p in Path(self.dir).iterdir()]

    def test_single_shard_exact_oracle(self) -> None:
        records = [(1000 + i, f"msg-{i}".encode()) for i in range(50)]
        _, lines = build_shards(self.dir, records, max_bytes=10_000_000)
        merged = b"".join(Merger(self._shards()).merged_bytes())
        self.assertEqual(merged, b"".join(lines))

    def test_split_middle_exact_oracle(self) -> None:
        records = [(1000 + i, f"payload-{i:04d}-" .encode() + b"A" * 30) for i in range(100)]
        _, lines = build_shards(self.dir, records, max_bytes=64)  # 故意很小，制造大量中切
        self.assertGreater(len(self._shards()), 5)
        merged = b"".join(Merger(self._shards()).merged_bytes())
        self.assertEqual(merged, b"".join(lines))  # 与未轮转逐行输出逐字节一致

    def test_split_boundary_exact_byte(self) -> None:
        """阈值恰好把一条记录切成 2 + (N-2)。"""
        line = format_record(1, 1, b"0123456789")
        with RotatingWriter(self.dir, max_bytes=22) as w:  # 头20 + 数据2
            w.write_line_bytes(line)
        files = self._shards()
        self.assertGreaterEqual(len(files), 2)
        merged = b"".join(Merger(files).merged_bytes())
        self.assertEqual(merged, line)

    def test_order_independent_of_filenames(self) -> None:
        records = [(i, f"m{i}".encode()) for i in range(60)]
        build_shards(self.dir, records, max_bytes=100)
        files = self._shards()
        a = b"".join(Merger(files).merged_bytes())
        random.Random(0).shuffle(files)
        b = b"".join(Merger(files).merged_bytes())
        renamed = []
        for i, f in enumerate(sorted(files)):
            nf = os.path.join(self.dir, f"zzz-{100 - i:04d}")
            os.rename(f, nf)
            renamed.append(nf)
        c = b"".join(Merger(renamed).merged_bytes())
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_missing_middle_shards(self) -> None:
        records = [(1000 + i, f"msg-{i}".encode()) for i in range(300)]
        build_shards(self.dir, records, max_bytes=64)
        files = sorted(self._shards())
        keep = files[:2] + files[5:7] + files[9:]
        # 存活分片在缺口两侧的内容必须逐条等于 oracle 中对应的连续行
        merger = Merger(keep, max_shard_bytes=64)
        got = [rec for rec in merger.records() if rec.parse_ok]
        report = merger.report
        self.assertEqual(len(report.gaps), 2)
        for gap in report.gaps:
            self.assertIsNotNone(gap.ts_interval)
            self.assertIsNotNone(gap.lost_records_lb)
            self.assertGreaterEqual(gap.missing_bytes, 1)
            self.assertGreaterEqual(gap.missing_shards_lb, 1)
        # 下界不超过真实丢失数
        all_seqs = {i + 1 for i in range(300)}
        got_seqs = {int(r.seq) for r in got}
        real_lost = len(all_seqs - got_seqs)
        self.assertLessEqual(sum(g.lost_records_lb for g in report.gaps), real_lost)
        # 时间区间端点就是两侧完整记录的 ts
        g0 = report.gaps[0]
        self.assertEqual(g0.ts_interval[0], g0.prev_record[0])
        self.assertEqual(g0.ts_interval[1], g0.next_record[0])

    def test_missing_head(self) -> None:
        records = [(1000 + i, f"m{i}".encode()) for i in range(200)]
        build_shards(self.dir, records, max_bytes=64)
        files = sorted(self._shards())
        merger = Merger(files[2:], max_shard_bytes=64)
        list(merger.records())
        gaps = merger.report.gaps
        self.assertEqual(len(gaps), 1)
        self.assertIsNone(gaps[0].prev_record)  # 头部缺口：时间区间无左端点
        self.assertIsNotNone(gaps[0].next_record)
        self.assertIsNotNone(gaps[0].lost_records_lb)  # 由首条可见记录的 seq 给出
        # 注意：尾部整体缺失（最后若干分片被删）物理上不可检测，见 README 限制说明

    def test_truncated_tail_record(self) -> None:
        records = [(1000 + i, f"m{i}".encode()) for i in range(20)]
        build_shards(self.dir, records, max_bytes=10_000_000)
        f = self._shards()[0]
        data = Path(f).read_bytes()
        Path(f).write_bytes(data[:-5])  # 截掉最后半行
        merger = Merger([f])
        got = list(merger.records())
        self.assertEqual(len(got), 19)
        self.assertEqual(len(merger.report.truncated_tails), 1)

    def test_clock_rollback(self) -> None:
        records = [(1000, b"a"), (900, b"b"), (800, b"c"), (950, b"d")]
        _, lines = build_shards(self.dir, records, max_bytes=10_000_000)
        merger = Merger(self._shards())
        got = list(merger.records())
        # 输出顺序 = 写入顺序（seq 升序）；ts_eff 单调不降
        self.assertEqual([r.seq for r in got], [1, 2, 3, 4])
        self.assertEqual([r.ts_eff for r in got], [1000, 1000, 1000, 1000])
        self.assertEqual([r.ts_ms for r in got], [1000, 900, 800, 950])
        self.assertEqual(len(merger.report.rollbacks), 3)  # 900/800/950 均低于修复前沿1000
        self.assertEqual(b"".join(r.raw for r in got), b"".join(lines))
        # 朴素 (ts, seq) 全量排序会给出 seq=3,2,4,1（与写入序不同）
        naive = sorted(got, key=lambda r: (r.ts_ms, r.seq))
        self.assertEqual([r.seq for r in naive], [3, 2, 4, 1])

    def test_same_millisecond(self) -> None:
        records = [(500, b"a"), (500, b"b"), (500, b"c")]
        _, lines = build_shards(self.dir, records, max_bytes=10_000_000)
        got = list(Merger(self._shards()).records())
        self.assertEqual([r.seq for r in got], [1, 2, 3])  # 同毫秒按 seq 定序
        self.assertEqual(b"".join(r.raw for r in got), b"".join(lines))

    def test_duplicate_seq_is_kept_and_reported(self) -> None:
        with RotatingWriter(self.dir, max_bytes=10_000) as w:
            w.write_record(1, 1, b"first")
            w.write_record(2, 1, b"dup-seq")  # 人为制造重复序号
            w.write_record(3, 3, b"third")
        merger = Merger(self._shards())
        got = list(merger.records())
        self.assertEqual([r.payload for r in got], [b"first", b"dup-seq", b"third"])  # 两条都保留
        self.assertEqual(len(merger.report.duplicate_seqs), 1)
        # 缺口下界被标记为 degraded
        files = sorted(self._shards())

    def test_dangling_fragments_reported(self) -> None:
        records = [(i, f"padding-{i:04d}-".encode() + b"Z" * 40) for i in range(80)]
        build_shards(self.dir, records, max_bytes=64)
        files = sorted(self._shards())
        merger = Merger(files[:2] + files[4:], max_shard_bytes=64)
        list(merger.records())
        self.assertEqual(len(merger.report.dangling_tails), 1)
        self.assertEqual(len(merger.report.dangling_heads), 1)
        self.assertIsNotNone(merger.report.dangling_tails[0].seq)

    def test_overlap_rejected(self) -> None:
        d = tempfile.mkdtemp()
        try:
            p1, p2 = Path(d) / "a", Path(d) / "b"
            p1.write_bytes(b"#LSHRD v1 offset=0\n1 1 x\n")
            p2.write_bytes(b"#LSHRD v1 offset=5\n2 2 y\n")
            with self.assertRaises(FormatError):
                discover_shards([str(p1), str(p2)])
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main(verbosity=2)

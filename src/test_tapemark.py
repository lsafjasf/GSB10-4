"""tapemark 自测：边界用例 + 确定性 + 顺序读写头。

运行: python3 test_tapemark.py  (或 python3 -m unittest -v)
"""

import io
import struct
import unittest

from tapemark import (
    HEADER_SIZE,
    FormatError,
    Kind,
    TapeReader,
    TruncatedVolume,
    encode_data,
    encode_eof,
    encode_eov,
    iter_events,
    scan,
)


class LimitedReader:
    """每次 read 最多返回 n 字节的文件对象，模拟记录跨底层读块边界。"""

    def __init__(self, data: bytes, limit: int):
        self._buf = io.BytesIO(data)
        self._limit = limit

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0 or size > self._limit:
            size = self._limit
        return self._buf.read(size)


def sample_image() -> bytes:
    """数据块 + 文件标记 + 内嵌标记字节的数据块 + 连续标记 + 卷结束。"""
    return b"".join([
        encode_data(b"hello volume"),                    # 0 DATA
        encode_eof(),                                     # 1 EOF
        encode_data(encode_eof() + encode_eov() + b"xy"), # 2 DATA(内嵌标记字节)
        encode_eof(),                                     # 3 EOF
        encode_eof(),                                     # 4 EOF (连续标记)
        encode_data(bytes(range(256)) * 4),               # 5 DATA
        encode_eov(),                                     # 6 EOV
    ])


def brief(events):
    return [(e.kind, e.offset, e.length, e.end) for e in events]


class TestMarks(unittest.TestCase):
    def test_empty_volume_reports_truncation(self):
        # 空卷：0 条完整记录，缺 8 字节记录头，最后完整边界 = 0
        with self.assertRaises(TruncatedVolume) as ctx:
            scan(b"")
        report = ctx.exception.report
        self.assertEqual(report.blocks_read, 0)
        self.assertEqual(report.missing_bytes, HEADER_SIZE)
        self.assertEqual(report.last_complete_end, 0)
        self.assertEqual(report.phase, "header")

    def test_single_block(self):
        image = encode_data(b"only block") + encode_eov()
        events = scan(image, want_payload=True)
        self.assertEqual([e.kind for e in events], [Kind.DATA, Kind.EOV])
        self.assertEqual(events[0].payload, b"only block")
        self.assertEqual(events[0].offset, 0)
        self.assertEqual(events[0].end, HEADER_SIZE + 10)

    def test_consecutive_marks(self):
        image = encode_eof() + encode_eof() + encode_eov()
        events = scan(image)
        self.assertEqual([e.kind for e in events],
                         [Kind.EOF, Kind.EOF, Kind.EOV])
        self.assertTrue(all(e.length == 0 for e in events))
        self.assertEqual([e.offset for e in events], [0, 8, 16])

    def test_mark_bytes_inside_data_not_misjudged(self):
        # 数据载荷原样包含 EOF/EOV 标记字节序列，不得误判
        payload = encode_eof() + encode_eov() + encode_eof()
        image = encode_data(payload) + encode_eov()
        events = scan(image, want_payload=True)
        self.assertEqual([e.kind for e in events], [Kind.DATA, Kind.EOV])
        self.assertEqual(events[0].payload, payload)
        self.assertEqual(events[0].length, 24)

    def test_mark_like_bytes_at_every_alignment(self):
        # 载荷在每种字节对齐下都含标记序列，结果仍只有一条 DATA
        for shift in range(HEADER_SIZE):
            payload = b"\xAA" * shift + encode_eov() + b"\xBB" * 3
            image = encode_data(payload) + encode_eov()
            kinds = [e.kind for e in scan(image)]
            self.assertEqual(kinds, [Kind.DATA, Kind.EOV], f"shift={shift}")

    def test_mark_spanning_read_block_boundary(self):
        # 记录头被底层读块任意切碎（1..9 字节/次），解析结果不变
        image = sample_image()
        expected = brief(scan(image))
        for limit in (1, 2, 3, 5, 7, 8, 9, 13):
            got = brief(iter_events(LimitedReader(image, limit)))
            self.assertEqual(got, expected, f"read limit={limit}")

    def test_truncated_mid_payload(self):
        # 记录头声明 100 字节载荷，实际只有 30：缺 70，已读 0 条
        image = struct.pack("<4sI", b"DATA", 100) + b"x" * 30
        with self.assertRaises(TruncatedVolume) as ctx:
            scan(image)
        report = ctx.exception.report
        self.assertEqual(report.blocks_read, 0)
        self.assertEqual(report.missing_bytes, 70)
        self.assertEqual(report.last_complete_end, 0)
        self.assertEqual(report.phase, "payload")

    def test_truncated_mid_header(self):
        # 完整数据块之后只有 5 字节残头：缺 3，已读 1 条，边界=块尾
        block = encode_data(b"abc")
        image = block + b"\x00" * 5
        with self.assertRaises(TruncatedVolume) as ctx:
            scan(image)
        report = ctx.exception.report
        self.assertEqual(report.blocks_read, 1)
        self.assertEqual(report.missing_bytes, HEADER_SIZE - 5)
        self.assertEqual(report.last_complete_end, len(block))
        self.assertEqual(report.phase, "header")

    def test_missing_eov_is_truncation_not_silent_end(self):
        # 只有数据块、没有卷结束标记：必须报告而非静默结束
        image = encode_data(b"abc")
        with self.assertRaises(TruncatedVolume) as ctx:
            scan(image)
        report = ctx.exception.report
        self.assertEqual(report.blocks_read, 1)
        self.assertEqual(report.missing_bytes, HEADER_SIZE)
        self.assertEqual(report.last_complete_end, len(image))

    def test_unknown_tag_is_format_error(self):
        with self.assertRaises(FormatError) as ctx:
            scan(struct.pack("<4sI", b"XXXX", 0))
        self.assertEqual(ctx.exception.offset, 0)

    def test_mark_with_nonzero_length_is_format_error(self):
        with self.assertRaises(FormatError):
            scan(struct.pack("<4sI", b"EOFM", 3) + b"abc")

    def test_stream_parser_matches_scan(self):
        image = sample_image()
        self.assertEqual(brief(iter_events(io.BytesIO(image))),
                         brief(scan(image)))

    def test_trailing_bytes_after_eov_ignored(self):
        image = encode_data(b"d") + encode_eov() + b"garbage after volume"
        events = scan(image)
        self.assertEqual(events[-1].kind, Kind.EOV)


class TestDeterminism(unittest.TestCase):
    def test_repeated_parse_identical(self):
        image = sample_image()
        first = brief(scan(image))
        for _ in range(5):
            self.assertEqual(brief(scan(image)), first)
            self.assertEqual(brief(iter_events(io.BytesIO(image))), first)

    def test_chunking_does_not_change_result(self):
        # 同一段数据、不同底层分块，事件序列逐字节一致
        image = sample_image()
        expected = brief(scan(image))
        for limit in (1, 4, 8, 64, 4096):
            self.assertEqual(brief(iter_events(LimitedReader(image, limit))),
                             expected)


class TestTapeReader(unittest.TestCase):
    def test_forward_backward_sequence(self):
        image = sample_image()
        reader = TapeReader(image)

        # 前进到底，记录每一步之后的位置与状态
        forward_trace = []
        while True:
            event = reader.step_forward()
            if event is None:
                break
            forward_trace.append((event.kind, event.offset,
                                  reader.position, reader.state()))
        self.assertEqual(len(forward_trace), 7)
        self.assertEqual(forward_trace[-1][3], "AFTER_EOV")
        self.assertEqual(reader.position, len(image))
        self.assertIsNone(reader.step_forward())  # EOV 之后无更多记录

        # 后退两步：应精确回到倒数第 2、3 条记录之前
        back1 = reader.step_back()
        self.assertEqual(back1.kind, Kind.EOV)
        self.assertEqual(reader.position, back1.offset)
        self.assertEqual(reader.state(), "AFTER_DATA")
        back2 = reader.step_back()
        self.assertEqual(back2.kind, Kind.DATA)
        self.assertEqual(reader.position, back2.offset)

        # 再前进：与首次解析同一位置的结果完全一致
        again = reader.step_forward()
        self.assertEqual((again.kind, again.offset, again.end),
                         (back2.kind, back2.offset, back2.end))

        # 一路退到卷首
        while reader.step_back() is not None:
            pass
        self.assertEqual(reader.position, 0)
        self.assertEqual(reader.state(), "BOT")
        self.assertIsNone(reader.step_back())

    def test_reader_position_sequence_matches_scan(self):
        image = sample_image()
        expected = [(e.kind, e.offset, e.end) for e in scan(image)]
        reader = TapeReader(image)
        got = []
        while True:
            event = reader.step_forward()
            if event is None:
                break
            got.append((event.kind, event.offset, event.end))
        self.assertEqual(got, expected)

    def test_reader_on_truncated_volume_raises(self):
        reader = TapeReader(encode_data(b"abc"))  # 无 EOV
        self.assertEqual(reader.step_forward().kind, Kind.DATA)
        with self.assertRaises(TruncatedVolume) as ctx:
            reader.step_forward()
        self.assertEqual(ctx.exception.report.blocks_read, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

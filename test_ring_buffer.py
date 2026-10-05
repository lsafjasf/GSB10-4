"""环形缓冲区自测（python3 -m unittest test_ring_buffer -v）。

覆盖：
- 空缓冲区 / 满缓冲区
- 单字节读写
- 跨回绕（wraparound）读写
- 一次读写跨越两段
- 与朴素大数组（bytearray）模型逐字节对拍，落盘 diff_test_data.txt
"""

import hashlib
import os
import random
import unittest

from ring_buffer import RingBuffer

HERE = os.path.dirname(os.path.abspath(__file__))
DIFF_FILE = os.path.join(HERE, "diff_test_data.txt")


class TestEmptyFull(unittest.TestCase):
    def test_empty_state(self):
        rb = RingBuffer(4)
        self.assertTrue(rb.is_empty())
        self.assertFalse(rb.is_full())
        self.assertEqual(rb.size, 0)
        self.assertEqual(rb.free, 4)
        self.assertEqual(rb.read(10), b"")            # 空读不报错，返回空
        self.assertEqual(rb.read_segments(10), [])
        self.assertEqual(rb.write_segments(b""), [])
        self.assertTrue(rb.check_invariant())

    def test_full_state(self):
        rb = RingBuffer(4)
        self.assertEqual(rb.write(b"ABCD"), 4)
        self.assertTrue(rb.is_full())
        self.assertEqual(rb.size, 4)
        self.assertEqual(rb.free, 0)
        self.assertEqual(rb.write(b"X"), 0)            # 满：写不进去，绝不覆盖
        self.assertEqual(rb.read(4), b"ABCD")
        self.assertTrue(rb.is_empty())

    def test_single_byte(self):
        rb = RingBuffer(1)
        self.assertEqual(rb.capacity, 1)
        self.assertEqual(rb.write(b"Z"), 1)
        self.assertTrue(rb.is_full())
        self.assertEqual(rb.write(b"Q"), 0)
        self.assertEqual(rb.read(1), b"Z")
        self.assertTrue(rb.is_empty())
        self.assertEqual(rb.read(1), b"")
        # 容量 1 下反复单字节收发，强制大量回绕
        for i in range(1000):
            self.assertEqual(rb.write(bytes([i % 256])), 1)
            self.assertEqual(rb.read(1), bytes([i % 256]))


class TestWraparound(unittest.TestCase):
    def test_write_read_across_boundary(self):
        cap = 8
        rb = RingBuffer(cap)
        self.assertEqual(rb.write(b"0123456"), 7)       # free=1
        self.assertEqual(rb.read(5), b"01234")          # 读游标移到 slot5
        # 此时空 6 格：slot7 一段 + slot0..4 一段，写入必回绕
        self.assertEqual(rb.write(b"ABCDEF"), 6)
        self.assertEqual(rb.read(8), b"56ABCDEF")       # 读同样回绕，顺序必须正确
        self.assertTrue(rb.is_empty())

    def test_segments_write(self):
        cap = 8
        rb = RingBuffer(cap)
        rb.write(b"0123456")
        rb.read(5)                                      # write_slot=7, read_slot=5
        segs = rb.write_segments(b"ABCDEF")
        self.assertEqual(segs, [(7, 1), (0, 5)])        # 1 字节到末尾，5 字节绕到头部
        # 两段物理存储拼成逻辑顺序
        self.assertEqual(rb.read(8), b"56ABCDEF")

    def test_segments_read(self):
        cap = 8
        rb = RingBuffer(cap)
        rb.write(b"0123456")
        rb.read(5)
        rb.write(b"ABCDEF")                             # 逻辑队列 = 56ABCDEF
        segs = rb.read_segments(8)
        self.assertEqual(segs, [b"56A", b"BCDEF"])      # 读跨物理边界拆两段
        self.assertEqual(b"".join(segs), b"56ABCDEF")

    def test_segments_limited_by_n(self):
        cap = 8
        rb = RingBuffer(cap)
        rb.write(b"01234567")
        rb.read(6)                                      # read_slot=6, 数据 67
        rb.write(b"XYZWU")                              # 填满 67XYZWU + 1
        segs = rb.read_segments(3)
        self.assertEqual(segs, [b"67", b"X"])          # n=3 时跨边界拆两段
        segs = rb.read_segments(4)
        self.assertEqual(segs, [b"YZWU"])              # 不跨边界只有一段
        self.assertEqual(rb.read(2), b"")              # 已读空


class TestBlocking(unittest.TestCase):
    """阻塞读写回归：写入量超过容量时必须分段唤醒，不得死锁。"""

    def test_large_write_with_small_capacity(self):
        import threading
        cap = 16
        rb = RingBuffer(cap)
        payload = bytes((i * 7 + 1) % 256 for i in range(10_000))
        got = []
        ready = threading.Event()

        def producer():
            ready.set()
            self.assertEqual(rb.write_blocking(payload), len(payload))

        def consumer():
            offset = 0
            while offset < len(payload):
                part = rb.read_blocking(100, timeout=5)
                self.assertTrue(part, "消费者等待超时 -> 回归到唤醒死锁 bug")
                self.assertEqual(part, payload[offset:offset + len(part)])
                offset += len(part)

        tp = threading.Thread(target=producer)
        tc = threading.Thread(target=consumer)
        tp.start(); tc.start()
        ready.wait(1)
        tp.join(6); tc.join(6)
        self.assertFalse(tp.is_alive(), "producer 死锁")
        self.assertFalse(tc.is_alive(), "consumer 死锁")
        self.assertTrue(rb.is_empty())

    def test_blocking_timeout(self):
        rb = RingBuffer(4)
        self.assertEqual(rb.read_blocking(1, timeout=0.05), b"")
        self.assertEqual(rb.write_blocking(b"ABCD", timeout=0.05), 4)
        self.assertEqual(rb.write_blocking(b"X", timeout=0.05), 0)


class TestDifferential(unittest.TestCase):
    """与朴素大数组模型逐字节对拍。"""

    def _run(self, ops: int, cap: int, seed: int, log_lines: int | None):
        rng = random.Random(seed)
        rb = RingBuffer(cap)
        model = bytearray()          # 朴素实现：无限大数组 + 头部游标
        log = []
        out_hash = hashlib.sha256()
        counts = {"write": 0, "read": 0, "seg_write": 0, "seg_read": 0}
        total_bytes = 0

        for i in range(ops):
            kind = rng.choice(("write", "write", "read", "read",
                               "seg_write", "seg_read"))
            if kind in ("write", "seg_write"):
                n = rng.randint(0, 2 * cap)
                data = bytes(rng.randrange(256) for _ in range(n))
                free = cap - len(model)
                if kind == "write":
                    got = rb.write(data)
                    segs = None
                else:
                    segs = rb.write_segments(data)
                    got = sum(length for _, length in segs)
                self.assertEqual(got, min(n, free))
                model += data[:got]
                counts[kind] += 1
                total_bytes += got
                rec = f"{i:07d} W n={n} accepted={got} size={rb.size}"
                if segs is not None:
                    rec += f" segs={segs}"
            else:
                n = rng.randint(0, 2 * cap)
                if kind == "read":
                    got = rb.read(n)
                    segs = None
                else:
                    segs = rb.read_segments(n)
                    got = b"".join(segs)
                expected = bytes(model[:n])
                self.assertEqual(got, expected)         # 逐字节对拍
                out_hash.update(got)
                del model[:len(got)]
                counts[kind] += 1
                rec = (f"{i:07d} R n={n} got={len(got)} "
                       f"hex={got[:8].hex()} size={rb.size}")
                if segs is not None:
                    rec += f" seglen={[len(s) for s in segs]}"

            self.assertEqual(rb.size, len(model))       # 占用量每步一致
            self.assertTrue(rb.check_invariant())
            if log_lines is not None and i < log_lines:
                log.append(rec)

        return counts, total_bytes, out_hash, log

    def test_differential_long(self):
        counts, total_bytes, out_hash, _ = self._run(
            ops=200_000, cap=64, seed=20261005, log_lines=None)
        # 再用不同容量/种子跑一轮
        counts2, total2, hash2, _ = self._run(
            ops=100_000, cap=37, seed=424242, log_lines=None)
        header = (
            "环形缓冲区 vs 朴素大数组 逐字节对拍数据\n"
            "说明: W=写入请求 R=读出请求; size=对拍时环形缓冲区占用量;\n"
            "      segs=[(slot,len),...] 物理分段; hex=读出内容前若干字节。\n"
            "规则: 每一步 read 结果必须与大数组模型逐字节相同, 且 size 一致。\n"
            "============================================================\n"
        )
        _, _, _, short_log = self._run(
            ops=5_000, cap=16, seed=777, log_lines=5_000)
        with open(DIFF_FILE, "w", encoding="utf-8") as f:
            f.write(header)
            f.write("[详细日志] cap=16 seed=777 ops=5000（全量记录）\n")
            f.write("\n".join(short_log) + "\n\n")
            f.write("[长程对拍汇总]\n")
            f.write(f"run1 cap=64 seed=20261005 ops=200000 {counts} "
                    f"bytes={total_bytes} sha256={out_hash.hexdigest()}\n")
            f.write(f"run2 cap=37 seed=424242 ops=100000 {counts2} "
                    f"bytes={total2} sha256={hash2.hexdigest()}\n")
            f.write("RESULT: PASS（所有操作逐字节一致，size 每步一致）\n")
        self.assertEqual(total_bytes > 0, True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

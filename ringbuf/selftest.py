"""环形缓冲区自测：边界 / 回绕分段 / 朴素实现对拍 / 生产消费并发压测。

运行：python3 selftest.py            （全部）
      python3 selftest.py basic      （仅边界 + 分段）
      python3 selftest.py diff       （仅对拍）
      python3 selftest.py concur     （仅并发压测）
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ringbuf import BlockingRingBuffer, BufferFull, RingBuffer


class NaiveRingModel:
    """朴素大数组参照实现：无限 bytearray 做队列，head 指向最旧未读字节。

    不回绕、不覆盖、行为即"无限大的 FIFO 字节队列"，用作对拍 oracle。
    """

    def __init__(self):
        self.store = bytearray()
        self.head = 0

    def readable(self) -> int:
        return len(self.store) - self.head

    def write(self, data: bytes) -> int:
        self.store += data
        return len(data)

    def read(self, n: int) -> bytes:
        n = min(n, self.readable())
        out = bytes(self.store[self.head : self.head + n])
        self.head += n
        if self.head > 1_000_000:  # 周期性清理已读前缀
            del self.store[: self.head]
            self.head = 0
        return out

    def peek_all(self) -> bytes:
        return bytes(self.store[self.head :])


class TestBoundaryAndWrap(unittest.TestCase):
    """空缓冲区、满缓冲区、单字节、跨回绕分段。"""

    def test_empty_buffer(self):
        rb = RingBuffer(4)
        self.assertTrue(rb.is_empty)
        self.assertFalse(rb.is_full)
        self.assertEqual(len(rb), 0)
        self.assertEqual(rb.free, 4)
        self.assertEqual(rb.read(10), b"")
        self.assertEqual(rb.read_segments(), [])
        rb.check_invariants()

    def test_full_buffer(self):
        rb = RingBuffer(4)
        self.assertEqual(rb.write(b"ABCD"), 4)
        self.assertTrue(rb.is_full)
        self.assertFalse(rb.is_empty)
        self.assertEqual(rb.free, 0)
        self.assertEqual(rb.write(b"X"), 0)  # 满时不得覆盖未读数据
        with self.assertRaises(BufferFull):
            rb.write_all(b"Y")
        rb.check_invariants()
        self.assertEqual(rb.read_all(), b"ABCD")
        self.assertTrue(rb.is_empty)

    def test_single_byte_cycles(self):
        rb = RingBuffer(3)
        sent = []
        got = []
        for i in range(200):
            b = bytes([i % 251 + 1])
            if not rb.is_full:
                self.assertEqual(rb.write(b), 1)
                sent.append(b)
                rb.check_invariants()
            if not rb.is_empty:
                got.append(rb.read(1))
                rb.check_invariants()
        got.append(rb.read_all())
        # 内容必须与发送顺序一致，无错位
        self.assertEqual(b"".join(got), b"".join(sent))

    def test_write_split_across_wrap(self):
        """r=4,w=5 时一次写入 6 字节 -> 3+3 两段回绕。"""
        rb = RingBuffer(8)
        self.assertEqual(rb.write(b"AAAAA"), 5)   # w: 0->5
        self.assertEqual(rb.read(4), b"AAAA")    # r: 0->4, n=1
        self.assertEqual(rb.free, 7)
        written = rb.write(b"BCDEFG")            # 槽位: 5,6,7,0,1,2
        self.assertEqual(written, 6)
        rb.check_invariants()

        segs = rb.read_segments()                # 读视图必须跨两段
        self.assertEqual(len(segs), 2)
        # 下标4是第一次留下的 A，5/6/7 = B C D，0/1/2 = E F G
        self.assertEqual(bytes(segs[0]), b"ABCD")  # r=4: A(4) B(5) C(6) D(7)
        self.assertEqual(bytes(segs[1]), b"EFG")   # w=3: E(0) F(1) G(2)
        self.assertEqual(rb.peek(7), b"ABCDEFG")
        # 跨段 read 也必须得到同样结果
        self.assertEqual(rb.read(7), b"ABCDEFG")
        self.assertTrue(rb.is_empty)
        rb.check_invariants()

    def test_exact_boundary_write(self):
        """写长度恰好等于到末尾的距离 -> 只一段且游标回绕到 0。"""
        rb = RingBuffer(8)
        rb.write(b"AAAAA")
        rb.read(5)
        self.assertEqual(rb.write(b"12345678"), 8)  # w=5，写 8 = 3 + 5
        rb.check_invariants()
        self.assertEqual(rb.read(3), b"123")
        rest = rb.read(5)                            # r=0? r=0: 剩余 5678...
        self.assertEqual(rest, b"45678")
        rb.check_invariants()


class TestDifferential(unittest.TestCase):
    """与朴素大数组实现逐字节对拍（随机操作 + 强制回绕）。"""

    def test_randomized_byte_for_byte(self):
        rng_seed = 20261005
        import random
        rng = random.Random(rng_seed)
        mismatches = 0
        total_bytes_checked = 0
        wrap_events = 0
        sample = deque(maxlen=6)  # 保存若干对拍样本供输出

        for trial in range(200):
            cap = rng.choice([1, 2, 3, 7, 8, 13, 64])
            rb = RingBuffer(cap)
            model = NaiveRingModel()
            for _ in range(300):
                if rng.random() < 0.55:  # 写
                    data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 12)))
                    before_w = rb._w
                    n = rb.write(data)  # 满时部分/零写入
                    model.write(data[:n])
                    if rb._w < before_w and n:
                        wrap_events += 1
                else:                   # 读
                    n = rng.randrange(0, 12)
                    got = rb.read(n)
                    want = model.read(len(got))
                    if got != want:
                        mismatches += 1
                    total_bytes_checked += len(got)
                # 逐字节对拍：环形中所有可读字节必须等于朴素队列
                a = rb.peek(len(rb))
                b = model.peek_all()
                if a != b:
                    mismatches += 1
                else:
                    total_bytes_checked += len(a)
                self.assertEqual(a, b)
                self.assertEqual(len(rb), model.readable())
                rb.check_invariants()
                if a and len(sample) < 6 and rb._n:
                    sample.append((cap, bytes(a).hex()))

        print("\n[对拍] seed=%d trials=200 操作/trial=300" % rng_seed)
        print("[对拍] 逐字节校验字节数=%d 回绕事件=%d 不一致=%d"
              % (total_bytes_checked, wrap_events, mismatches))
        print("[对拍] 样本（容量, 环形可读内容hex == 朴素队列hex）:")
        for cap, hx in sample:
            print("        cap=%2d  %s" % (cap, hx))
        self.assertEqual(mismatches, 0)
        self.assertGreater(wrap_events, 0)  # 确实覆盖了回绕


class TestConcurrent(unittest.TestCase):
    """多生产者/多消费者压测：不得覆盖未读数据，不得丢字节/错序。"""

    def test_mpsc_stress(self):
        producers = 4
        consumers = 4
        pkts_per_producer = 25_000
        pkt_size = 8
        cap = 53  # 故意不是包大小整数倍，强制部分写与回绕
        total_pkts = producers * pkts_per_producer

        q = BlockingRingBuffer(cap)
        next_seq = [0] * producers          # 每个生产者期望的下一个序号
        received = [0] * producers
        errors: list[str] = []
        stats_lock = threading.Lock()

        def producer(pid: int):
            for seq in range(pkts_per_producer):
                payload = ((seq * 1103515245 + pid * 12345) & 0xFFFFFF).to_bytes(3, "big")
                pkt = bytes([pid]) + seq.to_bytes(4, "big") + payload
                q.put(pkt)

        claimed = [0]  # 已被消费者认领（即将读取）的包数

        def consumer():
            while True:
                with stats_lock:
                    if claimed[0] >= total_pkts:
                        return
                    claimed[0] += 1  # 先认领，再读取：get 调用数恰为总包数
                pkt = q.get(pkt_size)
                pid = pkt[0]
                seq = int.from_bytes(pkt[1:5], "big")
                payload = pkt[5:8]
                expect_payload = ((seq * 1103515245 + pid * 12345) & 0xFFFFFF).to_bytes(3, "big")
                with stats_lock:
                    if payload != expect_payload:
                        errors.append(f"pid={pid} seq={seq} 载荷损坏（疑似覆盖）")
                    if seq != next_seq[pid]:
                        errors.append(
                            f"pid={pid} 期望seq={next_seq[pid]} 实际={seq}（错序/丢包/覆盖）"
                        )
                        next_seq[pid] = seq + 1
                    else:
                        next_seq[pid] = seq + 1
                    received[pid] += 1

        t0 = time.perf_counter()
        ps = [threading.Thread(target=producer, args=(i,)) for i in range(producers)]
        cs = [threading.Thread(target=consumer) for _ in range(consumers)]
        for t in ps + cs:
            t.start()
        for t in ps:
            t.join()
        for t in cs:
            t.join()
        elapsed = time.perf_counter() - t0

        print("\n[并发] 生产者=%d 消费者=%d 容量=%d 包=%d字节 总包数=%d"
              % (producers, consumers, cap, pkt_size, total_pkts))
        print("[并发] 每生产者收包=%s 总收包=%d"
              % (received, sum(received)))
        print("[并发] 错误（覆盖/丢包/错序/损坏）=%d  耗时=%.2fs  吞吐=%.0f 包/s (%.2f MB/s)"
              % (len(errors), elapsed, total_pkts / elapsed,
                 total_pkts * pkt_size / elapsed / 1e6))
        self.assertEqual(received, [pkts_per_producer] * producers)
        self.assertEqual(next_seq, [pkts_per_producer] * producers)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    mapping = {
        "basic": [TestBoundaryAndWrap],
        "diff": [TestDifferential],
        "concur": [TestConcurrent],
        "all": [TestBoundaryAndWrap, TestDifferential, TestConcurrent],
    }
    for cls in mapping[which]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(1 if result.failures or result.errors else 0)

"""aba_stack 的测试套件（unittest，仅标准库）。

覆盖：单线程、单生产者单消费者（SPSC）、多生产者多消费者（MPMC）、
版本号回绕（人为小模数）、与加锁实现对拍、回收时机安全性。

运行：python3 test_aba_stack.py -v
"""

import random
import threading
import unittest
from collections import Counter

from aba_stack import EMPTY, EpochReclaimer, LockedStack, LockFreeStack


def run_mpmc(stack, num_producers, num_consumers, per_producer):
    """并发压测骨架：返回所有消费者弹出的元素列表。"""
    popped_lists = [[] for _ in range(num_consumers)]
    producers_done = threading.Event()

    def producer(pid):
        for i in range(per_producer):
            stack.push((pid, i))  # 每个元素全局唯一

    def consumer(cid):
        out = popped_lists[cid]
        while True:
            v = stack.pop()
            if v is EMPTY:
                if producers_done.is_set():
                    return
                continue
            out.append(v)

    threads = [
        threading.Thread(target=producer, args=(p,)) for p in range(num_producers)
    ] + [
        threading.Thread(target=consumer, args=(c,)) for c in range(num_consumers)
    ]
    for t in threads:
        t.start()
    for t in threads[:num_producers]:
        t.join()
    producers_done.set()
    for t in threads[num_producers:]:
        t.join()
    return [v for lst in popped_lists for v in lst]


def check_conservation(testcase, popped, num_producers, per_producer, label):
    """元素守恒校验：不丢、不重、栈空。"""
    total = num_producers * per_producer
    counts = Counter(popped)
    duplicates = {k: c for k, c in counts.items() if c > 1}
    expected = {(p, i) for p in range(num_producers) for i in range(per_producer)}
    missing = expected - set(popped)
    print(
        "[守恒校验 %s] pushed=%d popped=%d distinct=%d "
        "duplicates=%d missing=%d" % (
            label, total, len(popped), len(counts), len(duplicates), len(missing),
        )
    )
    testcase.assertEqual(len(popped), total, "元素丢失")
    testcase.assertEqual(duplicates, {}, "元素重复")
    testcase.assertEqual(missing, set(), "元素缺失")


class TestSingleThread(unittest.TestCase):
    def test_basic_lifo(self):
        s = LockFreeStack()
        self.assertIs(s.pop(), EMPTY)
        for i in range(1000):
            s.push(i)
        out = [s.pop() for _ in range(1000)]
        self.assertEqual(out, list(range(999, -1, -1)))  # 严格后进先出
        self.assertIs(s.pop(), EMPTY)

    def test_interleaved(self):
        s = LockFreeStack()
        expect = []
        rng = random.Random(42)
        for _ in range(10000):
            if rng.random() < 0.5 or not expect:
                v = rng.randrange(1 << 30)
                s.push(v)
                expect.append(v)
            else:
                self.assertEqual(s.pop(), expect.pop())
        while expect:
            self.assertEqual(s.pop(), expect.pop())
        self.assertIs(s.pop(), EMPTY)


class TestDifferential(unittest.TestCase):
    """与加锁实现对拍：同一随机操作序列，每次弹出结果与最终栈序完全一致。"""

    def test_matches_locked_stack(self):
        for seed in range(5):
            rng = random.Random(seed)
            lf, lk = LockFreeStack(), LockedStack()
            ops = pushes = pops = 0
            for _ in range(20000):
                ops += 1
                if rng.random() < 0.6:
                    v = rng.randrange(1 << 30)
                    lf.push(v)
                    lk.push(v)
                    pushes += 1
                else:
                    a, b = lf.pop(), lk.pop()
                    pops += 1
                    self.assertEqual(a is EMPTY, b is EMPTY, "seed=%d" % seed)
                    if a is not EMPTY:
                        self.assertEqual(a, b, "seed=%d" % seed)
            drained = 0
            while True:
                a, b = lf.pop(), lk.pop()
                self.assertEqual(a is EMPTY, b is EMPTY)
                if a is EMPTY:
                    break
                self.assertEqual(a, b)
                drained += 1
            print(
                "[对拍 seed=%d] ops=%d pushes=%d pops=%d drained=%d "
                "逐次弹出结果与最终栈序完全一致" % (seed, ops, pushes, pops, drained)
            )


class TestSPSC(unittest.TestCase):
    def test_conservation(self):
        n = 100_000
        popped = run_mpmc(LockFreeStack(), 1, 1, n)
        check_conservation(self, popped, 1, n, "SPSC")


class TestMPMC(unittest.TestCase):
    def test_conservation(self):
        popped = run_mpmc(LockFreeStack(), 4, 4, 25_000)
        check_conservation(self, popped, 4, 25_000, "MPMC 4x4")


class TestTagWraparound(unittest.TestCase):
    """人为把版本号压窄（mod 2 / mod 256）强制回绕，验证延迟回收兜底。"""

    def test_tag_actually_wraps(self):
        s = LockFreeStack(tag_modulus=4)
        for i in range(100):
            s.push(i)
        head = s._head.load()
        self.assertLess(head.tag, 4)  # 版本号确实被模数截断、已经回绕多次
        for _ in range(100):
            s.pop()

    def test_conservation_under_wrap_mod2(self):
        popped = run_mpmc(LockFreeStack(tag_modulus=2), 2, 2, 10_000)
        check_conservation(self, popped, 2, 10_000, "wrap mod2 2x2")

    def test_conservation_under_wrap_mod256(self):
        popped = run_mpmc(LockFreeStack(tag_modulus=256), 4, 4, 10_000)
        check_conservation(self, popped, 4, 10_000, "wrap mod256 4x4")


class TestReclamationTiming(unittest.TestCase):
    """回收时机：有线程 pin 在旧纪元时不得回收；静默期后必须回收。"""

    def test_no_reclaim_while_reader_pinned(self):
        rec = EpochReclaimer()
        s = LockFreeStack(reclaimer=rec)
        s.push("x")
        pinned = threading.Event()
        release = threading.Event()
        done = threading.Event()

        def reader():
            rec.pin()  # 模拟一个停留在临界区内的慢读者
            pinned.set()
            release.wait(5)
            rec.unpin()
            done.set()

        t = threading.Thread(target=reader)
        t.start()
        self.assertTrue(pinned.wait(5))

        s.pop()  # 节点退役
        for _ in range(5):
            rec.reclaim_now()
        self.assertEqual(rec.retired, 1)
        self.assertEqual(rec.reclaimed, 0, "读者仍 pin 在旧纪元，提前回收！")
        print("[回收时机] 读者 pin 住期间: retired=1 reclaimed=0 (正确阻塞)")

        release.set()
        self.assertTrue(done.wait(5))
        t.join()
        for _ in range(5):
            rec.reclaim_now()
        self.assertEqual(rec.reclaimed, 1, "静默期后仍未回收")
        print("[回收时机] 读者离开后: reclaimed=1 (正确释放)")

    def test_reclaimed_nodes_are_recycled(self):
        rec = EpochReclaimer()
        s = LockFreeStack(reclaimer=rec)
        s.push("a")
        node = s._head.load().node
        s.pop()
        for _ in range(4):
            rec.reclaim_now()
        self.assertEqual(rec.reclaimed, 1)
        s.push("b")
        self.assertIs(s._head.load().node, node)  # 同一个对象被安全复用
        self.assertEqual(s.pop(), "b")
        print("[回收复用] 节点对象在静默期后被安全复用")


if __name__ == "__main__":
    unittest.main(verbosity=2)

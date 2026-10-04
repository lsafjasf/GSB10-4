"""水位连接池自测：python3 -m unittest test_watermark_pool -v"""
import random
import threading
import time
import unittest
from collections import deque

from watermark_pool import (ConnState, PoolExhausted, ReleasedConnError,
                            WatermarkPool)


class VirtualClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_pool(clock, **kw):
    closed = kw.pop("closed", None)
    return WatermarkPool(time_fn=clock,
                         close_fn=(closed.append if closed is not None else None),
                         **kw)


class IdleShrinkTests(unittest.TestCase):
    def test_only_idle_conns_closed_and_floor_respected(self):
        clock = VirtualClock()
        closed = []
        pool = make_pool(clock, min_size=4, max_size=32,
                         low_water=0.3, high_water=0.7,
                         shrink_step=2, idle_timeout=10.0, cooldown=1.0,
                         closed=closed)
        # 先借空触发扩容，直到持有 12 条连接
        leases = []
        while len(leases) < 12:
            cid = pool.try_acquire()
            if cid is None:
                clock.advance(1.1)
                pool.tick()
            else:
                leases.append(cid)
        self.assertEqual(12, pool.snapshot()["size"])
        for cid in leases:
            pool.release(cid)
        self.assertEqual(12, pool.snapshot()["idle"])
        # 空闲时长不足：不缩
        clock.advance(9.9)
        pool.tick()
        self.assertEqual(12, pool.snapshot()["size"])
        # 超过 idle_timeout：按 shrink_step、受 cooldown 节流
        clock.advance(0.2)
        pool.tick()
        self.assertEqual(10, pool.snapshot()["size"])
        for _ in range(20):
            clock.advance(1.1)
            pool.tick()
        self.assertEqual(pool.min_size, pool.snapshot()["size"])
        # 被关闭的全部是当时 in_flight == 0 的连接
        self.assertEqual(set(closed) & set(pool._conns), set())
        pool.check_invariants()

    def test_idle_boundary_exact_timeout_eligible(self):
        clock = VirtualClock()
        pool = make_pool(clock, min_size=2, max_size=16,
                         low_water=0.3, high_water=0.7,
                         shrink_step=4, max_shrink_ratio=1.0,
                         idle_timeout=10.0, cooldown=1.0)
        for _ in range(6):
            pool.release(pool.try_acquire())
        clock.advance(10.0)  # 恰好等于 idle_timeout，按 >= 判定
        pool.tick()
        self.assertEqual(2, pool.snapshot()["size"])


class FullLoadExpandTests(unittest.TestCase):
    def test_expansion_rate_limited_and_step_bounded(self):
        clock = VirtualClock()
        pool = make_pool(clock, min_size=4, max_size=64,
                         low_water=0.3, high_water=0.7,
                         expand_step=4, expand_rate=4, expand_window=1.0,
                         cooldown=0.5)
        busy = [pool.try_acquire() for _ in range(4)]
        sizes = []
        # 负载持续满载：每个 0.5s 评估一次，1 秒窗口最多 4 个新连接
        for _ in range(10):
            clock.advance(0.5)
            pool.tick()
            sizes.append(pool.snapshot()["size"])
        self.assertTrue(all(b <= a + 4 for a, b in zip(sizes, sizes[1:])), sizes)
        # 任何 1 秒窗口内新增不超过 expand_rate（令牌桶上界）
        self.assertLessEqual(sizes[2] - sizes[0], 4 + 4, sizes)
        pool.check_invariants()

    def test_cap_saturation_no_drop(self):
        clock = VirtualClock()
        pool = make_pool(clock, min_size=2, max_size=16,
                         low_water=0.3, high_water=0.7,
                         expand_step=8, expand_rate=16, expand_window=1.0,
                         cooldown=0.0)
        held = []
        waiting = deque()
        for _ in range(40):
            cid = pool.try_acquire()
            if cid is None:
                waiting.append(clock.t)
            else:
                held.append((cid, clock.t))
        for _ in range(6):
            clock.advance(1.0)
            pool.tick()
            while waiting and pool.snapshot()["idle"] > 0:
                cid = pool.try_acquire()
                self.assertIsNotNone(cid)
                held.append((cid, clock.t))
        self.assertEqual(16, pool.snapshot()["size"], "规模触顶")
        self.assertEqual(16, pool.snapshot()["busy"])
        self.assertTrue(len(waiting) > 0, "确实存在排队请求")
        # 负载回落：排队请求全部被服务（借到即还，模拟短任务），无丢弃
        for cid, _ in list(held):
            pool.release(cid)
        held.clear()
        served = 0
        while waiting:
            waiting.popleft()
            cid = pool.try_acquire()
            self.assertIsNotNone(cid, "回落连接必须服务排队请求，不得丢弃")
            pool.release(cid)
            served += 1
        self.assertGreater(served, 0)
        self.assertEqual(pool.stats["acquired"], pool.stats["released"])
        self.assertEqual(0, pool.stats["dropped"])
        pool.check_invariants()


class LongTailTests(unittest.TestCase):
    def test_drain_waits_for_in_flight_then_closes(self):
        clock = VirtualClock()
        pool = make_pool(clock, min_size=4, max_size=8,
                         low_water=0.3, high_water=0.7)
        tail = pool.try_acquire()
        others = [pool.try_acquire() for _ in range(3)]
        self.assertNotIn(None, others)
        # 长尾连接在途时被摘除：必须等请求完成，不得掐断
        pool.drain(tail)
        self.assertIn(tail, pool._conns)
        self.assertEqual(pool._conns[tail].state, ConnState.DRAINING)
        # 缩容期间 tick 多次，长尾连接仍在（在途保护）
        for cid in others:
            pool.release(cid)
        clock.advance(120.0)
        for _ in range(20):
            clock.advance(1.0)
            pool.tick()
        self.assertIn(tail, pool._conns, "在途请求未结清，连接不得关闭")
        self.assertEqual(1, pool._conns[tail].in_flight)
        pool.release(tail)  # 长尾请求安全结清
        self.assertNotIn(tail, pool._conns, "结清后立即关闭")
        self.assertEqual(pool.stats["dropped"], 0)
        pool.check_invariants()


class OscillationTests(unittest.TestCase):
    def test_hysteresis_and_cooldown_suppress_flapping(self):
        def run(low, high, cooldown):
            clock = VirtualClock()
            pool = make_pool(clock, min_size=4, max_size=64,
                             low_water=low, high_water=high,
                             expand_step=2, shrink_step=2,
                             expand_rate=8, expand_window=1.0,
                             idle_timeout=0.0, cooldown=cooldown)
            held = set()
            seq = []
            # 利用率在 ~0.5 附近反复横跳
            for i in range(60):
                clock.advance(0.2)
                if i % 2 == 0:
                    for _ in range(6):
                        cid = pool.try_acquire()
                        if cid is not None:
                            held.add(cid)
                else:
                    for cid in list(held)[:6]:
                        pool.release(cid)
                        held.discard(cid)
                pool.tick()
                seq.append(pool.snapshot()["size"])
            for cid in list(held):
                pool.release(cid)
            events = pool.stats["scale_up_events"] + pool.stats["scale_down_events"]
            return events, seq

        naive_events, naive_seq = run(0.49, 0.51, 0.0)
        good_events, good_seq = run(0.30, 0.70, 3.0)
        print("\n[防抖] 无防抖规模序列:", naive_seq[:20])
        print("[防抖] 受控规模序列:", good_seq[:20])
        print("[防抖] 调整事件 无防抖=%d 受控=%d" % (naive_events, good_events))
        self.assertGreater(naive_events, good_events)
        self.assertLess(good_events, naive_events // 2 + 1)
        # 受控序列不应出现连续两拍反向调整（抖动特征）
        flips = sum(1 for a, b in zip(good_seq, good_seq[1:]) if a != b)
        self.assertLessEqual(flips, 4)


class BoundaryTests(unittest.TestCase):
    def test_bad_watermark_args(self):
        with self.assertRaises(ValueError):
            WatermarkPool(low_water=0.8, high_water=0.2)
        with self.assertRaises(ValueError):
            WatermarkPool(low_water=0.5, high_water=0.5)
        with self.assertRaises(ValueError):
            WatermarkPool(min_size=8, max_size=4)

    def test_double_release_rejected(self):
        clock = VirtualClock()
        pool = make_pool(clock, min_size=1, max_size=2)
        cid = pool.try_acquire()
        pool.release(cid)
        with self.assertRaises(ReleasedConnError):
            pool.release(cid)
        self.assertEqual(pool.stats["released"], 1)
        pool.check_invariants()

    def test_acquire_timeout_no_half_grant(self):
        # 阻塞超时依赖真实时钟推进，此用例用默认 time_fn
        pool = WatermarkPool(min_size=1, max_size=1,
                             low_water=0.3, high_water=0.7, cooldown=100.0,
                             expand_step=0, expand_rate=0)
        cid = pool.try_acquire()

        def slow():
            with self.assertRaises(PoolExhausted):
                pool.acquire(timeout=0.05)
        t = threading.Thread(target=slow)
        t.start()
        t.join(2.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(pool.stats["acquire_timeouts"], 1)
        pool.release(cid)
        # 超时后连接仍可被再次借出，无半授予
        cid2 = pool.try_acquire()
        self.assertIsNotNone(cid2)
        pool.release(cid2)
        pool.check_invariants()

    def test_shrink_skips_busy_even_if_old(self):
        clock = VirtualClock()
        pool = make_pool(clock, min_size=1, max_size=8,
                         low_water=0.3, high_water=0.7,
                         shrink_step=8, max_shrink_ratio=1.0,
                         idle_timeout=5.0, cooldown=0.0)
        # 扩容到 4 条
        held = []
        while len(held) < 4:
            cid = pool.try_acquire()
            if cid is None:
                clock.advance(0.1)
                pool.tick()
            else:
                held.append(cid)
        busy = held.pop()          # 1 条长期在途
        for cid in held:
            pool.release(cid)      # 其余 3 条空闲
        clock.advance(100.0)       # 全部超过 idle_timeout
        pool.tick()                # util = 1/4 = 0.25 <= 0.3，触发缩容
        snap = pool.snapshot()
        self.assertEqual(1, snap["size"], "只缩空闲连接，在途连接保留")
        self.assertIn(busy, pool._conns)
        self.assertEqual(1, pool._conns[busy].in_flight)
        self.assertGreaterEqual(pool.stats["suppressed_shrink_busy"], 1)
        pool.release(busy)         # 长尾请求安全结清
        self.assertEqual(pool.stats["acquired"], pool.stats["released"])
        pool.check_invariants()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ThreadSafetyTests(unittest.TestCase):
    def test_concurrent_leases_never_overlap(self):
        pool = WatermarkPool(min_size=4, max_size=32,
                             low_water=0.3, high_water=0.7,
                             expand_step=4, expand_rate=16, expand_window=0.1,
                             cooldown=0.05, idle_timeout=0.2,
                             time_fn=time.monotonic)
        stop = threading.Event()

        def scheduler():
            while not stop.is_set():
                pool.tick()
                time.sleep(0.01)
        sched = threading.Thread(target=scheduler)
        sched.start()
        in_use = set()
        guard = threading.Lock()
        errors = []

        def worker():
            try:
                for _ in range(100):
                    with pool.acquire(timeout=2.0) as lease:
                        with guard:
                            self.assertNotIn(lease.conn_id, in_use)
                            in_use.add(lease.conn_id)
                        time.sleep(0.001)
                        with guard:
                            in_use.discard(lease.conn_id)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5.0)
            self.assertFalse(t.is_alive())
        stop.set()
        sched.join()
        self.assertEqual(errors, [])
        self.assertEqual(pool.stats["acquired"], pool.stats["released"])
        self.assertEqual(0, pool.stats["dropped"])
        pool.check_invariants()

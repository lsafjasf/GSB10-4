"""WatermarkPool 自测（unittest，纯标准库）。

覆盖：空闲缩放、满载扩容（速率上界）、上限触顶、请求长尾安全结清、
水位震荡抑制、随机对拍不变量、多线程冒烟。
运行：python3 test_watermark_pool.py -v
"""
import random
import threading
import unittest

from watermark_pool import PoolExhausted, WatermarkPool


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_pool(clock, **kw):
    defaults = dict(
        min_size=2,
        max_size=16,
        high_water=0.8,
        low_water=0.3,
        idle_timeout=5.0,
        up_burst=4,
        up_rate=8.0,
        down_step=1,
        down_interval=1.0,
        cooldown=2.0,
        time_fn=clock,
    )
    defaults.update(kw)
    return WatermarkPool(**defaults)


class TestIdleShrink(unittest.TestCase):
    """空闲缩放：低水位 + 空闲超时后，按步进限速向 min_size 收敛。"""

    def test_idle_shrink_stepwise_to_min(self):
        clock = FakeClock()
        log = []
        pool = make_pool(clock, up_burst=16, event_log=log)
        # 打满一波负载让池子长大
        handles = [pool.acquire(timeout=0) for _ in range(8)]
        clock.advance(0.5)
        for h in handles:
            h.release()
        size_after_load = pool.stats()["size"]
        self.assertGreaterEqual(size_after_load, 6)

        # 空闲 10s（超过 idle_timeout=5），逐秒 reap：每次最多缩 down_step=1
        clock.advance(10.0)
        sizes = [pool.stats()["size"]]
        for _ in range(20):
            pool.reap()
            sizes.append(pool.stats()["size"])
            clock.advance(1.0)
        # 单调不增、每步最多缩 1、最终收敛到 min_size=2 且不击穿
        for a, b in zip(sizes, sizes[1:]):
            self.assertGreaterEqual(a, b)
            self.assertLessEqual(a - b, 1)
        self.assertEqual(sizes[-1], 2)
        closes = [e for e in log if e[1] == "close"]
        self.assertTrue(closes)
        self.assertTrue(all(e[5] == "low_water" for e in closes))

    def test_no_shrink_when_busy_above_low_water(self):
        clock = FakeClock()
        pool = make_pool(clock, up_burst=16)
        handles = [pool.acquire(timeout=0) for _ in range(8)]
        size_busy = pool.stats()["size"]  # 8 繁忙 + 高水位预扩容
        self.assertGreaterEqual(size_busy, 8)
        clock.advance(100.0)  # 时间再久也不缩：繁忙占比 1.0 >= low_water
        self.assertEqual(pool.reap(), 0)
        self.assertEqual(pool.stats()["size"], size_busy)
        for h in handles:
            h.release()


class TestScaleUpRateLimit(unittest.TestCase):
    """满载扩容：突发受 up_burst 限制，之后每秒最多 up_rate 个。"""

    def test_burst_then_rate_limited(self):
        clock = FakeClock()
        pool = make_pool(clock, max_size=32, up_burst=4, up_rate=8.0)
        # t=0：2 个初始连接 + 4 个突发令牌 => 最多 6 个
        got = []
        for _ in range(6):
            got.append(pool.acquire(timeout=0))
        with self.assertRaises(PoolExhausted):
            pool.acquire(timeout=0)
        self.assertEqual(pool.stats()["size"], 6)
        # 令牌桶容量=up_burst=4：前进 1s 最多回充到 4 个 => 再扩 4 个
        clock.advance(1.0)
        for _ in range(4):
            got.append(pool.acquire(timeout=0))
        with self.assertRaises(PoolExhausted):
            pool.acquire(timeout=0)
        self.assertEqual(pool.stats()["size"], 10)
        # 任意窗口 [0, t] 内净增长 <= up_burst + up_rate * t
        self.assertLessEqual(10 - 2, 4 + 8.0 * 1.0)
        for h in got:
            h.release()

    def test_topup_only_above_high_water(self):
        clock = FakeClock()
        log = []
        pool = make_pool(clock, up_burst=32, up_rate=0.0, event_log=log)
        h1 = pool.acquire(timeout=0)  # busy 1/2 = 0.5 < 0.8，不预扩容
        self.assertEqual(pool.stats()["size"], 2)
        h2 = pool.acquire(timeout=0)  # busy 2/2 = 1.0 >= 0.8，触发预扩容
        self.assertEqual(pool.stats()["size"], 3)
        topups = [e for e in log if e[1] == "grow" and e[5] == "high_water"]
        self.assertEqual(len(topups), 1)
        h1.release()
        h2.release()


class TestCeiling(unittest.TestCase):
    """上限触顶：size 永不越过 max_size，多余获取收到背压。"""

    def test_hard_ceiling(self):
        clock = FakeClock()
        pool = make_pool(clock, max_size=8, up_burst=8, up_rate=100.0)
        got = [pool.acquire(timeout=0) for _ in range(8)]
        self.assertEqual(pool.stats()["size"], 8)
        for _ in range(4):
            with self.assertRaises(PoolExhausted):
                pool.acquire(timeout=0)
        self.assertEqual(pool.stats()["size"], 8)
        # 归还一个后立即可获取：触顶不丢请求，只是等待
        got[0].release()
        with pool.acquire(timeout=0) as h:
            self.assertLess(h.conn_id, 8)
        for h in got[1:]:
            h.release()


class TestLongTail(unittest.TestCase):
    """请求长尾：缩容绝不碰在途连接，长尾请求安全结清、不丢失。"""

    def test_inflight_conn_survives_shrink(self):
        clock = FakeClock()
        log = []
        pool = make_pool(clock, up_burst=16, idle_timeout=1.0,
                         cooldown=0.5, down_interval=0.5, event_log=log)
        handles = [pool.acquire(timeout=0) for _ in range(8)]
        long_tail = handles[0]          # 长尾请求一直持有
        tail_cid = long_tail.conn_id
        for h in handles[1:]:
            h.release()
        # 长时间空闲 + 多次 reap：池子缩到 min_size，但长尾连接必须存活
        for _ in range(40):
            clock.advance(1.0)
            pool.reap()
        st = pool.stats()
        # 长尾连接自身计入繁忙：busy/size = 1/3 >= low_water=0.3，
        # 水位在 size=3 处停住，不会在长尾未结清时缩到 min_size。
        self.assertEqual(st["size"], 3)
        self.assertEqual(st["in_flight"], 1)     # 长尾仍在途
        closed_cids = {e[2] for e in log if e[1] == "close"}
        self.assertNotIn(tail_cid, closed_cids)  # 在途连接未被关闭
        long_tail.release()
        self.assertEqual(pool.stats()["in_flight"], 0)


class TestOscillation(unittest.TestCase):
    """水位震荡：负载在水位线附近往复，冷却期 + 滞回带抑制抖动。"""

    def _drive(self, pool, clock, cycles=12):
        for _ in range(cycles):
            got = [pool.acquire(timeout=0) for _ in range(6)]  # 冲高越过水位
            for h in got:
                h.release()
            clock.advance(1.5)  # 短空闲，周期 < cooldown
            pool.reap()

    def test_cooldown_suppresses_flapping(self):
        clock_a, clock_b = FakeClock(), FakeClock()
        log_a, log_b = [], []
        pool_a = make_pool(clock_a, up_burst=4, up_rate=4.0, idle_timeout=1.0,
                           down_step=2, down_interval=0.5, cooldown=6.0,
                           event_log=log_a)
        pool_b = make_pool(clock_b, up_burst=4, up_rate=4.0, idle_timeout=1.0,
                           down_step=2, down_interval=0.5, cooldown=0.0,
                           event_log=log_b)
        self._drive(pool_a, clock_a)
        self._drive(pool_b, clock_b)
        scales_a = sum(1 for e in log_a if e[1] in ("grow", "close"))
        scales_b = sum(1 for e in log_b if e[1] in ("grow", "close"))
        flips = lambda log: sum(
            1 for x, y in zip(
                [e[1] for e in log if e[1] in ("grow", "close")],
                [e[1] for e in log if e[1] in ("grow", "close")][1:],
            ) if x != y
        )
        # 抑制结果：扩缩事件数与 缩->扩 翻转次数都降至 1/3 以下
        self.assertLessEqual(scales_a * 3, scales_b)
        self.assertLessEqual(flips(log_a) * 3, flips(log_b))


class TestDuelOracle(unittest.TestCase):
    """小规模随机对拍：与参照模型逐步比对，请求不丢、不变量成立。"""

    def test_random_ops_match_oracle(self):
        rng = random.Random(20261006)
        clock = FakeClock()
        pool = make_pool(clock, max_size=32, up_burst=8, up_rate=16.0,
                         idle_timeout=2.0, cooldown=1.0, down_interval=0.5)
        live = {}          # rid -> handle
        next_rid = 0
        completed = 0
        backpressure = 0
        for _ in range(20000):
            clock.advance(rng.random() * 0.05)
            if live and rng.random() < 0.5:
                rid = rng.choice(list(live))
                live.pop(rid).release()
                completed += 1
            else:
                try:
                    h = pool.acquire(timeout=0)
                except PoolExhausted:
                    backpressure += 1
                    continue
                live[next_rid] = h
                next_rid += 1
            st = pool.stats()
            self.assertEqual(st["in_flight"], len(live))       # 与参照模型一致
            self.assertGreaterEqual(st["size"], 2)
            self.assertLessEqual(st["size"], 32)
            self.assertGreaterEqual(st["tokens"], 0.0)
        for h in live.values():
            h.release()
            completed += 1
        self.assertEqual(pool.stats()["in_flight"], 0)
        self.assertEqual(completed, next_rid)                  # 全部结清，无丢失


class TestThreads(unittest.TestCase):
    """多线程冒烟：8 线程并发获取/归还，结束后在途归零。"""

    def test_concurrent_smoke(self):
        pool = WatermarkPool(min_size=2, max_size=32, up_burst=8, up_rate=64.0,
                             idle_timeout=0.2, cooldown=0.1, down_interval=0.05)
        errors = []

        def worker():
            try:
                for _ in range(3000):
                    with pool.acquire(timeout=5.0):
                        pass
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        st = pool.stats()
        self.assertEqual(st["in_flight"], 0)
        self.assertLessEqual(st["size"], 32)


if __name__ == "__main__":
    unittest.main(verbosity=2)

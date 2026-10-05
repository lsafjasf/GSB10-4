"""fairloop 自测。所有逻辑时间均由 FakeClock 注入，结果完全确定。

运行：python3 -m unittest -v test_fairloop
"""

import json
import time
import unittest

from fairloop import (
    BUCKET_COUNT,
    EventLoop,
    FakeClock,
    FairnessMonitor,
    Histogram,
    analyze,
)

MS = 1_000_000  # 1 毫秒 = 1e6 纳秒


def make_loop():
    clock = FakeClock()
    monitor = FairnessMonitor(clock.time_ns)
    loop = EventLoop(clock.time_ns, clock.advance, monitor)
    return clock, monitor, loop


def busy(clock, ns):
    """生成一个“占用循环 ns 纳秒”的回调（推进注入时钟模拟工作）。"""

    def _work():
        clock.advance(ns)

    return _work


class TestAllShortTasks(unittest.TestCase):
    """场景一：全为短任务，循环应当被判为公平。"""

    def test_all_short(self):
        clock, monitor, loop = make_loop()
        for i in range(1000):
            loop.call_soon(busy(clock, 1 * MS), name="short_task")
        loop.run()
        report = monitor.report()

        self.assertEqual(report.events, 1000)
        self.assertEqual(report.idle_ns, 0)
        # 单次占用：全部落在 [2^19, 2^20) 桶（1ms 所在桶）
        self.assertEqual(report.block.max_ns, 1 * MS)
        self.assertEqual(report.block.p50_ns, 1 << 19)
        self.assertEqual(report.block.p99_ns, 1 << 19)
        # 延迟：队列里排在后面的回调会等前面的，但 p50 有界
        self.assertLess(report.delay.p50_ns, 600 * MS)
        # 拖累清单只有一个注册点，平均占用 1ms
        self.assertEqual(len(report.offenders), 1)
        self.assertEqual(report.offenders[0].invocations, 1000)
        self.assertEqual(report.offenders[0].mean_block_ns, 1 * MS)


class TestSingleLongTask(unittest.TestCase):
    """场景二：单个超长任务，必须被精确定位并排在拖累清单首位。"""

    def test_single_long_task(self):
        clock, monitor, loop = make_loop()
        for _ in range(10):
            loop.call_soon(busy(clock, 1 * MS), name="short_task",
                           site="jobs.py:7")
        loop.call_soon(busy(clock, 500 * MS), name="heavy_batch",
                       site="jobs.py:42")
        for _ in range(10):
            loop.call_soon(busy(clock, 1 * MS), name="short_task",
                           site="jobs.py:7")
        loop.run()
        report = monitor.report()

        top = report.offenders[0]
        self.assertEqual(top.name, "heavy_batch")
        self.assertEqual(top.site, "jobs.py:42")  # 定位到注册点
        self.assertEqual(top.invocations, 1)
        self.assertEqual(top.max_block_ns, 500 * MS)
        self.assertGreater(top.block_share, 0.9)  # 一个回调占了 >90% 占用

        # 平均值会“洗白”长任务：mean 仅 ~25ms，但 max/p99 必须暴露它
        self.assertLess(report.block.mean_ns, 30 * MS)
        self.assertEqual(report.block.max_ns, 500 * MS)
        self.assertGreaterEqual(report.block.p99_ns, 1 << 28)  # >=256ms 桶

        # 排在长任务后面的短任务延迟被抬高
        short = next(o for o in report.offenders if o.name == "short_task")
        self.assertGreaterEqual(short.max_delay_ns, 500 * MS)


class TestPeriodicMediumTasks(unittest.TestCase):
    """场景三：周期性中等任务（每 10ms 一次、每次 5ms），按累计占用归因。"""

    def test_periodic_medium(self):
        clock, monitor, loop = make_loop()

        def periodic():
            clock.advance(5 * MS)
            if clock.now_ns < 200 * MS:
                loop.call_later(5 * MS, periodic, name="periodic",
                                site="timer.py:10")

        loop.call_later(0, periodic, name="periodic", site="timer.py:10")
        # 短任务散布在时间轴上，与周期任务交错执行
        for i in range(20):
            loop.call_later(i * 10 * MS, busy(clock, 1 * MS),
                            name="short_task")
        loop.run()
        report = monitor.report()

        top = report.offenders[0]
        self.assertEqual(top.name, "periodic")
        self.assertEqual(top.site, "timer.py:10")
        self.assertGreaterEqual(top.invocations, 20)
        # 单次只有 5ms（不算“长任务”），但累计占用最大 → 排第一
        self.assertEqual(top.max_block_ns, 5 * MS)
        self.assertGreater(top.total_block_ns, 100 * MS)


class TestIdleSlices(unittest.TestCase):
    """场景四：大量空闲时间片。空闲不算不公平，延迟应接近 0。"""

    def test_idle_dominates(self):
        clock, monitor, loop = make_loop()
        for i in range(10):
            loop.call_later(i * 100 * MS, busy(clock, 1 * MS),
                            name="sparse_task")
        loop.run()
        report = monitor.report()

        self.assertEqual(report.events, 10)
        self.assertGreater(report.idle_share, 0.9)  # >90% 时间在空闲
        self.assertEqual(report.delay.max_ns, 0)    # 到点就跑，零延迟
        self.assertEqual(report.block.max_ns, 1 * MS)


class TestDeterminism(unittest.TestCase):
    """同一段运行记录重复分析必须一致；同一场景跑两遍也必须一致。"""

    def _scenario(self):
        clock, monitor, loop = make_loop()
        for i in range(200):
            loop.call_later((i % 7) * MS, busy(clock, (i % 5) * MS),
                            name="task_%d" % (i % 3))
        loop.run()
        return monitor

    def test_same_record_same_result(self):
        monitor = self._scenario()
        first = analyze(monitor).to_dict()
        second = analyze(monitor).to_dict()
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_same_scenario_same_record(self):
        a = self._scenario().report().to_dict()
        b = self._scenario().report().to_dict()
        self.assertEqual(a, b)
        # 对拍数据打印出来，便于人工核对
        print("\n[determinism] events=%d busy=%dns idle=%dns p99=%dns"
              % (a["events"], a["busy_ns"], a["idle_ns"],
                 a["block"]["p99_ns"]))


class TestOverheadBound(unittest.TestCase):
    """检查器开销有上界：同负载开/关监控对比，每回调附加开销有硬上限。"""

    def test_overhead_per_callback_bounded(self):
        n = 50_000

        def run_once(with_monitor):
            clock = FakeClock()
            monitor = FairnessMonitor(clock.time_ns) if with_monitor else None
            loop = EventLoop(clock.time_ns, clock.advance, monitor)
            for _ in range(n):
                loop.call_soon(lambda: None, name="noop")
            start = time.perf_counter()
            loop.run()
            return time.perf_counter() - start

        # 预热后取 3 次最小值，降低抖动
        run_once(False)
        run_once(True)
        off = min(run_once(False) for _ in range(3))
        on = min(run_once(True) for _ in range(3))
        per_cb_ns = (on - off) / n * 1e9
        print("\n[overhead] n=%d off=%.3fs on=%.3fs per-callback=%.0fns"
              % (n, off, on, per_cb_ns))
        # 硬上界：每个回调附加开销 < 20µs（典型实测 < 2µs）
        self.assertLess(per_cb_ns, 20_000)


class TestEdgeCases(unittest.TestCase):
    def test_empty_run(self):
        _, monitor, loop = make_loop()
        loop.run()
        report = monitor.report()
        self.assertEqual(report.events, 0)
        self.assertEqual(report.wall_ns, 0)
        self.assertEqual(report.offenders, [])
        self.assertEqual(report.block.p99_ns, 0)

    def test_zero_duration_callback(self):
        clock, monitor, loop = make_loop()
        loop.call_soon(lambda: None, name="instant")
        loop.run()
        report = monitor.report()
        self.assertEqual(report.events, 1)
        self.assertEqual(report.block.max_ns, 0)

    def test_exception_still_recorded(self):
        clock, monitor, loop = make_loop()

        def boom():
            clock.advance(3 * MS)
            raise RuntimeError("boom")

        loop.call_soon(boom, name="boom")
        with self.assertRaises(RuntimeError):
            loop.run()
        report = monitor.report()  # 崩溃的回调也必须被记账
        self.assertEqual(report.events, 1)
        self.assertEqual(report.offenders[0].name, "boom")
        self.assertEqual(report.offenders[0].max_block_ns, 3 * MS)

    def test_early_fire_delay_clamped(self):
        # 时钟被外部回拨/提前触发时，负延迟截断为 0，不出负数
        clock, monitor, loop = make_loop()
        loop.call_later(10 * MS, busy(clock, 1 * MS), name="t")
        scheduled, _seq, key, cb = loop._heap[0]
        token = monitor.begin(key, scheduled)  # 不推进时钟，提前 10ms 触发
        cb()
        monitor.end(token)
        self.assertEqual(monitor.report().delay.max_ns, 0)

    def test_histogram_percentile(self):
        h = Histogram()
        for v in (1, 2, 4, 8, 16):
            h.add(v)
        self.assertEqual(h.count, 5)
        self.assertEqual(h.maximum, 16)
        self.assertEqual(h.percentile(0), 1)
        self.assertEqual(h.percentile(50), 4)
        self.assertEqual(h.percentile(100), 16)
        with self.assertRaises(ValueError):
            h.percentile(101)

    def test_registration_site_auto_captured(self):
        _, monitor, loop = make_loop()
        loop.call_soon(busy(FakeClock(), 0), name="auto_site")
        loop.run()
        site = monitor.report().offenders[0].site
        self.assertIn("test_fairloop.py:", site)


if __name__ == "__main__":
    unittest.main(verbosity=2)

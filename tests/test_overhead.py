"""检查器开销上界：开启监控 vs 关闭监控的耗时对比。

上界口径：监控开启后，每次回调执行的额外成本为 O(1)
（两次时钟读数 + 一次 list.append）。测试断言总开销倍率低于
一个宽松常数（10x），并把实测倍率打印出来供报告引用。
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fairloop import FairLoop, FairnessMonitor, VirtualClock

N = 200_000
REPEATS = 5


def _run_loop(with_monitor):
    clock = VirtualClock()
    monitor = FairnessMonitor() if with_monitor else None
    loop = FairLoop(clock, monitor)
    for i in range(N):
        loop.schedule(lambda: 0.001, name=f"t#{i}", site="bench:tick")
    start = time.perf_counter()
    loop.drain()
    return time.perf_counter() - start


class TestOverhead(unittest.TestCase):
    def test_monitor_overhead_bounded(self):
        baseline = min(_run_loop(False) for _ in range(REPEATS))
        monitored = min(_run_loop(True) for _ in range(REPEATS))
        ratio = monitored / baseline if baseline > 0 else 1.0
        per_event_ns = (monitored - baseline) / N * 1e9
        print(f"\n[overhead] baseline={baseline:.3f}s monitored={monitored:.3f}s "
              f"ratio={ratio:.2f}x extra={per_event_ns:.0f}ns/event (n={N})")
        self.assertLess(ratio, 10.0, "monitor overhead exceeds 10x bound")


if __name__ == "__main__":
    unittest.main()

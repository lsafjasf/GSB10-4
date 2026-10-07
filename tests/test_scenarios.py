"""四类标准场景的度量与归因断言。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fairloop import analyze
from fairloop.scenarios import (
    scenario_short_tasks,
    scenario_one_hog,
    scenario_periodic_medium,
    scenario_idle_slices,
)


class TestShortTasks(unittest.TestCase):
    def test_all_short(self):
        records = scenario_short_tasks(n=1000, tick=0.001)
        report = analyze(records)
        self.assertEqual(report.total_runs, 1000)
        self.assertAlmostEqual(report.duration["max"], 0.001)
        self.assertAlmostEqual(report.duration["p50"], 0.001)
        # 队列在 t=0 被预填满：第 i 个任务延迟 i*tick
        self.assertAlmostEqual(report.delay["p50"], 0.5)
        self.assertAlmostEqual(report.delay["max"], 0.999)
        self.assertAlmostEqual(report.idle_time, 0.0)
        self.assertAlmostEqual(report.utilization, 1.0)
        # 等待按占用分摊给排在前面的每个任务：队首分摊最多
        top = report.culprits[0]
        self.assertEqual(top.name, "short#0")
        self.assertAlmostEqual(top.total_induced_wait, 0.001 * 999)
        self.assertEqual(top.victims, 999)
        self.assertAlmostEqual(report.worst_offender[0], 0.001 * 999)


class TestOneHog(unittest.TestCase):
    def test_hog_is_identified(self):
        records = scenario_one_hog(n_short=100, hog=5.0, tick=0.001)
        report = analyze(records, stall_threshold=0.1)
        top = report.culprits[0]
        self.assertEqual(top.name, "hog")
        self.assertEqual(top.site, "scenario:hog")
        self.assertAlmostEqual(top.max_occupied, 5.0)
        self.assertEqual(top.victims, 100)
        # 每个排队任务的等待窗口都完整覆盖 hog 的 5s 占用
        self.assertAlmostEqual(top.total_induced_wait, 100 * 5.0)
        self.assertEqual(report.stalled_runs, 100)
        self.assertEqual(report.worst_offender[1], "hog")
        self.assertAlmostEqual(report.delay["max"], 5.0 + 99 * 0.001)
        # 其余回调的拖累与 hog 相比可忽略（第二名 < hog 的 1%）
        self.assertLess(report.culprits[1].total_induced_wait,
                        top.total_induced_wait / 100)


class TestPeriodicMedium(unittest.TestCase):
    def test_periodic(self):
        records = scenario_periodic_medium(periods=20, cost=0.05,
                                           tick=0.001, per_period=5)
        report = analyze(records)
        names = {c.name: c for c in report.culprits}
        medium = names["periodic-medium"]
        self.assertEqual(medium.runs, 20)
        self.assertAlmostEqual(medium.max_occupied, 0.05)
        # 每期拖住 5 个短任务；medium 重注册自身时也排在前一个 medium 后面
        self.assertEqual(medium.victims, 20 * 5 + 19)
        self.assertAlmostEqual(medium.total_induced_wait,
                               20 * 5 * 0.05 + 19 * 0.05)
        self.assertEqual(report.culprits[0].name, "periodic-medium")


class TestIdleSlices(unittest.TestCase):
    def test_idle(self):
        records = scenario_idle_slices(n=50, tick=0.001, idle=0.1)
        report = analyze(records)
        self.assertEqual(report.total_runs, 50)
        self.assertAlmostEqual(report.busy_time, 50 * 0.001)
        self.assertAlmostEqual(report.idle_time, 49 * 0.1)
        self.assertAlmostEqual(report.utilization, (50 * 0.001) / (49 * 0.101 + 0.001))
        # 每个回调入队即执行（队列已空），零延迟、零拖累
        self.assertAlmostEqual(report.delay["max"], 0.0)
        self.assertIsNone(report.worst_offender)
        self.assertEqual(sum(c.victims for c in report.culprits), 0)


if __name__ == "__main__":
    unittest.main()

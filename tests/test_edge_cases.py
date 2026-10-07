"""边界用例：空记录、零时长、负时长、就绪晚于开始、并列排序、单个回调。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fairloop import analyze, RunRecord
from fairloop.analyzer import dist_stats, _percentile


class TestEdgeCases(unittest.TestCase):
    def test_empty(self):
        report = analyze([])
        self.assertEqual(report.total_runs, 0)
        self.assertEqual(report.culprits, ())
        self.assertIsNone(report.worst_offender)
        self.assertEqual(report.utilization, 0.0)

    def test_single_run(self):
        rec = RunRecord(0, "solo", "site:solo", 3.0, 3.0, 3.5)
        report = analyze([rec])
        self.assertEqual(report.total_runs, 1)
        self.assertAlmostEqual(report.duration["max"], 0.5)
        self.assertAlmostEqual(report.delay["max"], 0.0)
        self.assertAlmostEqual(report.idle_time, 0.0)
        self.assertIsNone(report.worst_offender)

    def test_zero_duration_tasks(self):
        # 零时长事件不会互相拖累
        recs = [
            RunRecord(0, "a", "s:a", 0.0, 0.0, 0.0),
            RunRecord(1, "b", "s:b", 0.0, 0.0, 0.0),
            RunRecord(2, "c", "s:c", 0.0, 0.0, 0.0),
        ]
        report = analyze(recs)
        self.assertAlmostEqual(report.busy_time, 0.0)
        self.assertIsNone(report.worst_offender)
        self.assertEqual(sum(c.victims for c in report.culprits), 0)

    def test_negative_duration_rejected(self):
        with self.assertRaises(ValueError):
            analyze([RunRecord(0, "x", "s:x", 0.0, 1.0, 0.5)])

    def test_start_before_ready_rejected(self):
        with self.assertRaises(ValueError):
            analyze([RunRecord(0, "x", "s:x", 2.0, 1.0, 1.5)])

    def test_quantiles_nearest_rank(self):
        vals = list(range(10))  # 0..9
        stats = dist_stats(vals)
        self.assertEqual(stats["min"], 0)
        self.assertEqual(stats["p50"], 5)
        self.assertEqual(stats["p90"], 8)
        self.assertEqual(stats["p99"], 9)
        self.assertEqual(stats["max"], 9)
        self.assertEqual(stats["mean"], 4.5)
        self.assertEqual(dist_stats([])["count"], 0)

    def test_tie_break_order_is_stable(self):
        # 两个回调拖累完全相同 -> 按 (name, site) 字典序
        recs = [
            RunRecord(0, "x", "s:x", 0.0, 0.0, 1.0),
            RunRecord(1, "y", "s:y", 0.5, 1.0, 1.0),
            RunRecord(2, "z", "s:z", 1.0, 1.0, 2.0),
            RunRecord(3, "w", "s:w", 1.5, 2.0, 2.0),
        ]
        report = analyze(recs)
        names = [c.name for c in report.culprits[:2]]
        self.assertEqual(names, ["x", "z"])
        self.assertAlmostEqual(report.culprits[0].total_induced_wait, 0.5)

    def test_stalled_count(self):
        recs = [
            RunRecord(0, "h", "s:h", 0.0, 0.0, 10.0),
            RunRecord(1, "a", "s:a", 0.0, 10.0, 10.01),
            RunRecord(2, "b", "s:b", 0.0, 10.01, 10.02),
        ]
        self.assertEqual(analyze(recs, stall_threshold=1.0).stalled_runs, 2)
        self.assertEqual(analyze(recs, stall_threshold=0.0).stalled_runs, 2)
        self.assertEqual(analyze(recs, stall_threshold=100.0).stalled_runs, 0)


if __name__ == "__main__":
    unittest.main()

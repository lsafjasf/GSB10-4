"""一致性：
1. 同一份运行记录重复分析结果完全一致（含 JSON 逐字节一致）；
2. 扫描线归因 vs 朴素 O(n^2) 参照实现对拍；
3. 黄金文件 golden/hog_report.json 对拍。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fairloop import analyze, RunRecord
from fairloop.scenarios import scenario_one_hog


def brute_force_attribution(records):
    """朴素参照：每个等待窗口 vs 每个执行窗口直接求重叠。"""
    runs = sorted(records, key=lambda r: (
        r.start_ts, r.end_ts, r.ready_ts, r.reg_id, r.name, r.site))
    out = {}
    for w in runs:
        if w.start_ts <= w.ready_ts:
            continue
        for o in runs:
            if o.end_ts <= o.start_ts:
                continue
            overlap = min(o.end_ts, w.start_ts) - max(o.start_ts, w.ready_ts)
            if overlap > 0:
                ent = out.setdefault((o.name, o.site), [0.0, 0])
                ent[0] += overlap
                ent[1] += 1
    return out


def _synthetic_trace():
    """手工构造的确定性轨迹（含重入与空闲）。"""
    return [
        RunRecord(0, "A", "site:A", 0.0, 0.0, 2.0),
        RunRecord(1, "B", "site:B", 1.0, 2.0, 2.1),
        RunRecord(2, "C", "site:C", 1.5, 2.1, 2.2),
        # 空闲到 5.0
        RunRecord(3, "D", "site:D", 5.0, 5.0, 5.3),
        RunRecord(4, "E", "site:E", 5.1, 5.3, 5.31),
    ]


class TestDeterminism(unittest.TestCase):
    def test_repeated_analysis_identical(self):
        for trace in (_synthetic_trace(), scenario_one_hog()):
            r1 = analyze(trace).to_json()
            r2 = analyze(trace).to_json()
            self.assertEqual(r1, r2)

    def test_replay_trace_identical(self):
        # 重新跑一遍场景（虚拟时钟），报告仍一致
        self.assertEqual(
            analyze(scenario_one_hog()).to_json(),
            analyze(scenario_one_hog()).to_json(),
        )

    def test_shuffled_input_same_result(self):
        trace = _synthetic_trace()
        shuffled = [trace[i] for i in (3, 0, 4, 1, 2)]
        self.assertEqual(analyze(trace).to_json(), analyze(shuffled).to_json())

    def test_cross_check_brute_force(self):
        for trace in (_synthetic_trace(), scenario_one_hog(n_short=30)):
            report = analyze(trace)
            expect = brute_force_attribution(trace)
            for c in report.culprits:
                got = expect.get((c.name, c.site), [0.0, 0])
                self.assertAlmostEqual(c.total_induced_wait, got[0],
                                       msg=f"induced mismatch for {c.name}")
                self.assertEqual(c.victims, got[1],
                                 msg=f"victims mismatch for {c.name}")

    def test_synthetic_attribution_values(self):
        report = analyze(_synthetic_trace())
        by_name = {c.name: c for c in report.culprits}
        # B 等待 (1,2] 全在 A 窗口内 -> 1.0；C 等待 (1.5,2.1] -> A 0.5 + B 0.1
        self.assertAlmostEqual(by_name["A"].total_induced_wait, 1.5)
        self.assertEqual(by_name["A"].victims, 2)
        self.assertAlmostEqual(by_name["B"].total_induced_wait, 0.1)
        self.assertEqual(by_name["B"].victims, 1)
        # E 等待 (5.1,5.3] 全在 D 窗口内 -> 0.2
        self.assertAlmostEqual(by_name["D"].total_induced_wait, 0.2)
        self.assertEqual(by_name["D"].victims, 1)
        self.assertAlmostEqual(report.idle_time, 5.0 - 2.2)
        self.assertEqual(report.worst_offender[1], "A")

    def test_golden_file(self):
        golden_path = os.path.join(os.path.dirname(__file__),
                                   "..", "golden", "hog_report.json")
        with open(golden_path) as fh:
            golden = json.load(fh)
        self.assertEqual(golden, json.loads(analyze(scenario_one_hog()).to_json()))


if __name__ == "__main__":
    unittest.main()

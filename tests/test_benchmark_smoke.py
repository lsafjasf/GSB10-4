"""压测逻辑的冒烟测试（小规模，保证 CI 快速）。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bench  # noqa: E402


class TestBenchmarkSmoke(unittest.TestCase):
    def test_smoke_2000(self):
        scenarios = bench.gen_scenarios(2000, seed=1)
        lat1, wall, counters = bench.execute(scenarios)
        lat2, _, _ = bench.execute(bench.gen_scenarios(2000, seed=1))
        self.assertEqual(len(lat1), 2000)
        self.assertEqual(lat1, lat2)                      # 可复现
        self.assertTrue(all(0.0 <= v <= 0.1 + 1e-9 for v in lat1))
        self.assertEqual(counters["events_fired"], 2000)  # 每次迭代恰好一个超时事件
        self.assertEqual(len(wall), 2000)


if __name__ == "__main__":
    unittest.main()

"""100 万个回调：分析耗时与内存峰值。

轨迹由确定性模式直接合成（不依赖真实时钟）：
- 99% 短任务（1ms），1% 中等任务（50ms），另有 1 个 5s 超长任务；
- 每个长/中任务后面紧跟一批被拖住的短任务，用于验证归因规模。
"""

import os
import sys
import time
import tracemalloc
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fairloop import RunRecord, analyze

N = 1_000_000
HOG_VICTIMS = 5_000


def synthesize(n=N):
    """确定性合成 n 条执行记录。

    普通回调在前一个回调开始执行时就绪（delay = 前者的占用时长）；
    在中点插入一个 5s 超长任务，其窗口内就绪 5000 个受害者。
    """
    records = []
    now = 0.0
    prev_start = 0.0
    reg = 0
    i = 0
    while i < n:
        if i == n // 2:
            hog_start = now
            records.append(RunRecord(reg, "hog", "site:hog",
                                     prev_start, now, now + 5.0))
            reg += 1
            i += 1
            now += 5.0
            prev_start = hog_start
            for _ in range(min(HOG_VICTIMS, n - i)):
                records.append(RunRecord(reg, "victim", "site:victim",
                                         hog_start + 0.001, now, now + 0.001))
                reg += 1
                i += 1
                now += 0.001
            prev_start = now - 0.001
            continue
        if i % 100 == 0:
            dur, name = 0.05, "medium"
        else:
            dur, name = 0.001, "short"
        records.append(RunRecord(reg, name, f"site:{name}",
                                 prev_start, now, now + dur))
        prev_start = now
        now += dur
        reg += 1
        i += 1
    return records


class TestMillion(unittest.TestCase):
    def test_million_callbacks(self):
        records = synthesize()
        self.assertEqual(len(records), N)
        # 耗时：无 tracemalloc 干扰
        start = time.perf_counter()
        report = analyze(records)
        elapsed = time.perf_counter() - start
        # 内存峰值：单独跑一次，tracemalloc 只用于量化分析期增量
        import copy
        snapshot_input = list(records)
        tracemalloc.start()
        analyze(snapshot_input)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"\n[million] n={N} analyze={elapsed:.2f}s "
              f"peak_mem={peak / 1024 / 1024:.1f}MiB")
        by_name = {c.name: c for c in report.culprits}
        self.assertEqual(report.total_runs, N)
        self.assertEqual(by_name["hog"].runs, 1)
        self.assertAlmostEqual(by_name["hog"].max_occupied, 5.0)
        # hog 之后的短任务全部就绪于 hog 窗口内 -> 受害者众多
        self.assertGreater(by_name["hog"].victims, 1000)
        self.assertEqual(report.culprits[0].name, "hog")
        # 上界断言（宽松）：分析应为秒级、内存 GiB 级以下
        self.assertLess(elapsed, 120.0)
        self.assertLess(peak, 4 * 1024**3)


if __name__ == "__main__":
    unittest.main()

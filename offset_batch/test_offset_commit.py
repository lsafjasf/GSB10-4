"""自测：严格递增 / 乱序 / 空洞 / 失败回退 / 对拍 / 崩溃恢复。"""

import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from offset_commit import BatchResult, OffsetTable, WalCommitter, run_batch  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


class ReferenceModel:
    """逐条提交的朴素参考实现（用 set 求最长连续前缀）。"""

    def __init__(self) -> None:
        self.done = set()
        self.base = 0

    def commit_one(self, offset: int) -> int:
        self.done.add(offset)
        while self.base in self.done:
            self.base += 1
        return self.base


class TestOffsetTable(unittest.TestCase):
    def test_strictly_increasing(self) -> None:
        t = OffsetTable()
        bases = []
        for off in range(5):
            t.mark_done(off)
            bases.append(t.advance())
        self.assertEqual(bases, [1, 2, 3, 4, 5])
        self.assertEqual(t.intervals, [])

    def test_out_of_order_within_prefix(self) -> None:
        t = OffsetTable()
        for off in [4, 2, 3, 1, 0]:          # 乱序完成 0..4
            t.mark_done(off)
        self.assertEqual(t.advance(), 5)      # 凑齐前缀后一次推进
        self.assertEqual(t.intervals, [])

    def test_hole_blocks_advance(self) -> None:
        t = OffsetTable()
        for off in [0, 1, 4, 5, 8]:           # 空洞：2、3、6、7
            t.mark_done(off)
        self.assertEqual(t.pending_prefix(), [0, 1])
        self.assertEqual(t.advance(), 2)      # 只能停在空洞 2 之前
        self.assertEqual(t.hole(), 2)
        self.assertEqual(t.intervals, [[4, 5], [8, 8]])  # 后面位点保持暂存

        t.mark_done(3)                        # 2 仍然缺失，不能借 3 跳过
        self.assertEqual(t.advance(), 2)
        t.mark_done(2)
        self.assertEqual(t.advance(), 6)      # 2,3 补齐后 4,5 一起推进
        self.assertEqual(t.hole(), 6)

    def test_duplicate_and_below_base_ignored(self) -> None:
        t = OffsetTable()
        self.assertTrue(t.mark_done(0))
        self.assertFalse(t.mark_done(0))      # 重复
        t.advance()
        self.assertFalse(t.mark_done(0))      # 已提交位点再次完成

    def test_gap_fill_merges_three_segments(self) -> None:
        t = OffsetTable()
        for off in [0, 2, 4]:
            t.mark_done(off)
        t.mark_done(1)
        t.mark_done(3)
        self.assertEqual(t.advance(), 5)

    def test_starts_from_nonzero_base(self) -> None:
        t = OffsetTable(base=100)
        t.mark_done(100)
        t.mark_done(102)
        self.assertEqual(t.advance(), 101)
        self.assertEqual(t.hole(), 101)


class TestRollback(unittest.TestCase):
    def test_failure_rolls_back_current_batch_only(self) -> None:
        t = OffsetTable()
        run_batch(t, [0, 1, 2], lambda x: None)
        self.assertEqual(t.base, 3)

        calls = []

        def handler(off: int) -> None:
            calls.append(off)
            if off == 5:
                raise RuntimeError("boom")

        result = run_batch(t, [3, 4, 5, 6], handler)
        self.assertIsInstance(result, BatchResult)
        self.assertFalse(result.ok)
        self.assertEqual(result.rolled_back, [3, 4])  # 回退范围尽量小
        self.assertEqual(result.failed_at, 5)
        self.assertEqual(result.remaining, [6])
        self.assertEqual(str(result.error), "boom")
        self.assertEqual(t.base, 3)                   # 失败批完全没并入
        self.assertEqual(t.intervals, [])

        result2 = run_batch(t, [3, 4, 5, 6], lambda x: None)
        self.assertTrue(result2.ok)
        self.assertEqual(result2.committed, [3, 4, 5, 6])
        self.assertEqual(t.advance(), 7)

    def test_rollback_after_out_of_order_staged(self) -> None:
        t = OffsetTable()
        result = run_batch(t, [10, 11], lambda x: None)
        self.assertTrue(result.ok)
        self.assertEqual(t.base, 0)                   # 前面有空洞，不推进
        self.assertEqual(t.intervals, [[10, 11]])


class TestWalRecovery(unittest.TestCase):
    def test_replay_restores_base_and_pending(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "wal.log")
            c = WalCommitter(path)
            for off in [0, 1, 2, 4]:
                c.mark_done(off)
            c.commit()                              # base=3, 4 仍暂存
            c.close()

            r = WalCommitter.recover(path)
            self.assertEqual(r.table.base, 3)
            self.assertEqual(r.table.intervals, [[4, 4]])
            r.mark_done(3)
            self.assertEqual(r.commit(), 5)
            r.close()

    def test_trailing_partial_line_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "wal.log")
            with open(path, "wb") as fh:
                fh.write(b"D 0\nD 1\nC 2\nD 3")    # 末行被写一半
            r = WalCommitter.recover(path)
            self.assertEqual(r.table.base, 2)
            self.assertEqual(r.table.intervals, [])
            r.close()


class TestDifferential(unittest.TestCase):
    """批量/乱序推进 vs 逐条提交参考实现：每一步高水位必须一致。"""

    def test_random_sequences(self) -> None:
        rng = random.Random(20261005)
        traces = []
        total_steps = 0
        for case in range(400):
            n = rng.randrange(1, 120)
            seq = []
            for _ in range(rng.randrange(n, 3 * n + 1)):
                seq.append(rng.randrange(0, n + 5))  # 含重复、含空洞(n..n+4)
            ref = ReferenceModel()
            table = OffsetTable()
            sample = {"case": case, "n": n, "seq": seq[:12], "steps": []}
            for step, off in enumerate(seq):
                table.mark_done(off)
                got = table.advance()
                want = ref.commit_one(off)
                self.assertEqual(got, want, f"case={case} step={step} off={off}")
                total_steps += 1
                if case < 5 and step < 8:
                    sample["steps"].append({"off": off, "base": got})
            traces.append(sample)
        with open(os.path.join(HERE, "diff_test_data.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"cases": len(traces), "steps": total_steps,
                       "rule": "每步 OffsetTable.advance() == 逐条提交参考实现",
                       "samples": traces}, fh, ensure_ascii=False, indent=2)


class TestCrashRecovery(unittest.TestCase):
    N = 300

    def _run_child(self, wal: str, log: str, sleep: str) -> subprocess.Popen:
        env = dict(os.environ, CHILD_SLEEP_EACH=sleep)
        return subprocess.Popen(
            [sys.executable, os.path.join(HERE, "crash_child.py"), wal, log, str(self.N)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_sigkill_then_restart_skips_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            wal = os.path.join(d, "wal.log")
            log = os.path.join(d, "processed.log")

            proc = self._run_child(wal, log, "0.002")
            rng = random.Random(7)
            time.sleep(rng.uniform(0.1, 0.4))  # 让子进程跑进写入循环中段
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()

            # 恢复并跑完
            proc2 = self._run_child(wal, log, "0")
            out, err = proc2.communicate(timeout=60)
            self.assertEqual(proc2.returncode, 0, err)

            with open(log) as fh:
                processed = [int(x) for x in fh]
            self.assertEqual(sorted(set(processed)), list(range(self.N)),
                             "存在被跳过的位点")
            # 每次（重）启动内部必须严格递增且连续；段间只允许回退到已提交高水位
            runs, run = [], [processed[0]]
            for off in processed[1:]:
                if off <= run[-1]:
                    runs.append(run)
                    run = []
                run.append(off)
            runs.append(run)
            for r in runs:
                self.assertEqual(r, list(range(r[0], r[0] + len(r))),
                                 "单次运行内出现跳位点")
            self.assertLessEqual(len(runs), 2, "只重启了一次")
            duplicates = len(processed) - len(set(processed))
            # 高水位每 64 个位点落一次检查点，崩溃点至多重放一个检查点窗口
            self.assertLessEqual(duplicates, 64, "重放不应超过一个检查点窗口")
            self.assertGreater(len(processed), self.N, "崩溃应发生在处理中段")
            self.assertIn(f"final_base={self.N}", out)

            # 第三次重启：没有任何位点需要重放处理
            proc3 = self._run_child(wal, log, "0")
            out3, err3 = proc3.communicate(timeout=60)
            self.assertIn(f"start={self.N} final_base={self.N}", out3, err3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

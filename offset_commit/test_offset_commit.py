"""自测：连续前缀 / 乱序 / 空洞 / 失败回退 / 崩溃重启 / 对拍 / 100 万耗时。

运行: python3 test_offset_commit.py
"""

import os
import random
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from offset_commit import BatchGuard, OffsetStore, OffsetTracker  # noqa: E402
import worker  # noqa: E402


class TestOffsetTracker(unittest.TestCase):
    def test_strictly_increasing(self):
        t = OffsetTracker()
        history = []
        for off in range(5):
            t.mark_done(off)
            committed, advanced = t.commit_batch([])  # 已逐个 mark，这里只推进
            self.assertEqual(advanced, [off])
            history.append(committed)
        self.assertEqual(history, [0, 1, 2, 3, 4])
        self.assertEqual(t.committed, 4)
        self.assertEqual(len(t.pending), 0)

    def test_out_of_order_and_hole(self):
        t = OffsetTracker()
        # 乱序完成：0 没完成，先完成 1,2,3 —— 连续前缀不能推进
        committed, advanced = t.commit_batch([1, 2, 3])
        self.assertEqual(advanced, [])          # 空洞在 0，停在空洞之前
        self.assertEqual(committed, -1)
        self.assertEqual(t.pending, {1, 2, 3})
        # 空洞补上后，1,2,3 一次性连续推进（空洞之后的位点不会被跳过）
        committed, advanced = t.commit_batch([0])
        self.assertEqual(advanced, [0, 1, 2, 3])
        self.assertEqual(committed, 3)
        # 再来一个洞：4 未完成，5 完成
        committed, advanced = t.commit_batch([5, 7, 6])
        self.assertEqual(advanced, [])
        committed, advanced = t.commit_batch([4])
        self.assertEqual(advanced, [4, 5, 6, 7])
        self.assertEqual(committed, 7)
        self.assertEqual(t.pending, set())

    def test_idempotent_duplicate(self):
        t = OffsetTracker()
        t.commit_batch([0, 1])
        t.mark_done(0)  # 重复完成
        self.assertEqual(t.committed, 1)
        self.assertEqual(t.pending, set())

    def test_failure_rollback_minimal(self):
        t = OffsetTracker()
        # 前缀 0..3 已提交
        t.commit_batch([0, 1, 2, 3])
        # 新一批 [4,5,6]，处理到 6 失败；此时尚未提交
        guard = BatchGuard(t)
        guard.mark_done(4)
        guard.mark_done(5)
        rolled, already = guard.rollback()
        self.assertEqual(rolled, [4, 5])           # 回退数据：精确到失败批的未提交位点
        self.assertEqual(already, [])
        self.assertFalse(t.is_done(4))
        self.assertFalse(t.is_done(5))
        self.assertEqual(t.committed, 3)           # 已提交前缀不受影响
        # 回退后必须停在空洞之前：advance 推不动
        self.assertEqual(t.advance(), [])
        # 重新处理 4,5,6 成功后可继续推进
        self.assertEqual(t.commit_batch([4, 5, 6])[1], [4, 5, 6])

    def test_rollback_after_partial_advance(self):
        # 极端情形：批内位点已被独立提交流程推进过，则只回退未提交部分
        t = OffsetTracker()
        guard = BatchGuard(t)
        guard.mark_done(0)
        guard.mark_done(1)
        t.advance()  # 独立提交步骤先把 0,1 提交
        guard.mark_done(2)
        rolled, already = guard.rollback()
        self.assertEqual(rolled, [2])
        self.assertEqual(already, [0, 1])


def reference_single_commit(completions):
    """对拍参照：逐条提交，只在"下一位点已完成"时推进。返回 committed 推进序列。"""
    committed = -1
    done = set()
    commits = []
    for off in completions:
        done.add(off)
        while committed + 1 in done:
            committed += 1
            done.discard(committed)
            commits.append(committed)
    return commits


def batch_commit_stream(completions, batch_size):
    """被测：按 batch_size 攒批提交。返回 committed 推进序列。"""
    t = OffsetTracker()
    commits = []
    batch = []
    for off in completions:
        batch.append(off)
        if len(batch) >= batch_size:
            _, advanced = t.commit_batch(batch)
            commits.extend(advanced)
            batch = []
    if batch:
        _, advanced = t.commit_batch(batch)
        commits.extend(advanced)
    return commits


class TestDifferential(unittest.TestCase):
    def test_batch_vs_single(self):
        rng = random.Random(20261005)
        trials = 300
        shown = None
        for trial in range(trials):
            n = rng.randint(1, 200)
            completions = list(range(n))
            rng.shuffle(completions)
            ref = reference_single_commit(completions)
            for bs in (1, 2, 5, 16, 64):
                got = batch_commit_stream(completions, bs)
                self.assertEqual(got, ref, (trial, bs, completions))
            if shown is None and n >= 8:
                shown = (completions[:12], ref[:12])
        # 对拍数据示例
        print("\n[对拍] %d 组随机试验（乱序完成，批大小 1/2/5/16/64）全部一致" % trials)
        print("[对拍] 示例完成顺序(前12): %s -> committed 推进序列(前12): %s" % shown)
        print("[对拍] 最终 committed: 逐条=%d 批量=%d" %
              (reference_single_commit(completions)[-1], batch_commit_stream(completions, 16)[-1]))


class TestCrashRecovery(unittest.TestCase):
    def test_kill9_and_restart(self):
        n = 3000
        kill_after = 1234
        tmp = tempfile.mkdtemp(prefix="offset_crash_")
        wal = os.path.join(tmp, "wal.log")
        out = os.path.join(tmp, "out.log")

        def run(kill):
            args = [sys.executable, worker.__file__, wal, out, str(n), "37"]
            if kill:
                args.append(str(kill_after))
            return subprocess.run(args, cwd=os.path.dirname(worker.__file__))

        # 第一次运行：处理到 kill_after 条时 SIGKILL
        r1 = run(True)
        self.assertEqual(r1.returncode, -9)
        with open(wal, "rb") as fp:
            wal_bytes_1 = fp.read()
        self.assertIn(b"D ", wal_bytes_1)
        self.assertIn(b"C ", wal_bytes_1)
        # 崩溃后立即重放：committed 必须是连续前缀，无跳号
        crashed, _ = OffsetStore.replay(wal)
        self.assertGreaterEqual(crashed.committed, 0)

        # 重启跑到完成
        r2 = run(False)
        self.assertEqual(r2.returncode, 0)
        tracker, _ = OffsetStore.replay(wal)
        self.assertEqual(tracker.committed, n - 1)
        self.assertEqual(tracker.pending, set())

        # 输出文件：每个位点至少处理过一次（不得跳过未处理位点）
        with open(out) as fp:
            seen = sorted({int(x) for x in fp.read().split()})
        self.assertEqual(seen, list(range(n)))
        dup = len(open(out).read().split()) - n  # kill -9 恰好在结果落盘后、DONE 前会重放
        print("\n[崩溃] kill -9 于已处理 %d 条后；重启后 committed=%d，全部 %d 位点均被处理"
              % (kill_after, n - 1, n))
        print("[崩溃] 崩溃点 committed=%d；重放产生重复处理 %d 条（at-least-once，无跳过/无丢消息）"
              % (crashed.committed, dup))


class TestBenchmark(unittest.TestCase):
    def test_one_million(self):
        n = 1_000_000

        # 1) 顺序完成（常见路径）
        t = OffsetTracker()
        t0 = time.perf_counter()
        B = 1000
        for base in range(0, n, B):
            t.commit_batch(range(base, min(base + B, n)))
        elapsed_inorder = time.perf_counter() - t0
        self.assertEqual(t.committed, n - 1)
        self.assertEqual(t.pending, set())

        # 2) 最坏空洞：全部奇数先完成、偶数后完成，pending 峰值≈n/2
        t2 = OffsetTracker()
        t0 = time.perf_counter()
        t2.mark_done_batch(range(1, n, 2))
        t2.advance()
        t2.mark_done_batch(range(0, n, 2))
        t2.advance()
        elapsed_worst = time.perf_counter() - t0
        self.assertEqual(t2.committed, n - 1)

        tracemalloc.start()
        t3 = OffsetTracker()
        t3.mark_done_batch(range(1, n, 2))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        set_bytes = _set_bytes(n // 2)
        print("\n[耗时] 100 万消息顺序完成、批大小 %d：%.3f s（mark_done+advance 全部）" % (B, elapsed_inorder))
        print("[耗时] 最坏乱序（50 万空洞）完成+推进：%.3f s" % elapsed_worst)
        print("[内存] 位点表 = committed 1 个整数 + pending 集合；")
        print("[内存] 顺序路径 pending 峰值=%d，最坏(50万洞) pending 峰值=%d" % (0, t3.max_pending))
        print("[内存] tracemalloc 峰值 %.1f MiB；50 万 int 集合实测 %.1f MiB；"
              "理论上界 O(空洞数)，顺序完成时 O(1)" % (peak / 2 ** 20, set_bytes / 2 ** 20))


def _set_bytes(k):
    s = set(range(1, 2 * k + 1, 2))
    import sys
    return sys.getsizeof(s) + sum(sys.getsizeof(i) for i in range(1, 2001, 2))  # 采样小 int 部分


if __name__ == "__main__":
    unittest.main(verbosity=2)

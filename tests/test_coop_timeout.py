"""协作式超时运行时的单元自测（仅标准库 unittest）。

运行：python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coop_timeout import (  # noqa: E402
    Kind,
    ResourceLedger,
    make_failing_task,
    make_no_checkpoint_task,
    make_resource_task,
    run,
)


class TestNoTimeout(unittest.TestCase):
    """未超时：正常完成，结果与清理都符合预期。"""

    def test_ok_path(self):
        ledger = ResourceLedger()
        res = run(make_resource_task(ledger))
        o = res.outcome
        self.assertIs(o.kind, Kind.OK)
        self.assertEqual(o.value, {"result": "done", "steps": 4})
        self.assertEqual(o.phase, "SUBMIT")
        self.assertIsNone(o.latency)
        self.assertEqual(o.released, ("B", "A"))  # 逆序释放
        self.assertEqual(ledger.held(), [])       # 无泄漏
        self.assertEqual(res.clock.now, 9.0)      # 0..7 任务 + 2 清理
        self.assertEqual(o.deferred, 0)
        self.assertEqual(o.late, 0)


class TestExactTimeout(unittest.TestCase):
    """刚好超时：到期时刻恰好等于某个检查点/提交时刻。"""

    def test_deadline_equals_commit_time(self):
        # 提交时刻 t=7，deadline=7：平局归中断 -> TIMEOUT，零延迟
        ledger = ResourceLedger()
        o = run(make_resource_task(ledger), deadline=7.0).outcome
        self.assertIs(o.kind, Kind.TIMEOUT)
        self.assertEqual(o.phase, "SUBMIT")
        self.assertEqual(o.signal_at, 7.0)
        self.assertEqual(o.responded_at, 7.0)
        self.assertEqual(o.latency, 0.0)
        self.assertEqual(o.released, ("B", "A"))
        self.assertEqual(ledger.held(), [])

    def test_deadline_equals_work_checkpoint(self):
        # deadline=4 恰好是工作步检查点：零延迟响应
        ledger = ResourceLedger()
        o = run(make_resource_task(ledger), deadline=4.0).outcome
        self.assertIs(o.kind, Kind.TIMEOUT)
        self.assertEqual(o.phase, "WORK")
        self.assertEqual(o.latency, 0.0)

    def test_just_after_commit_is_ok(self):
        # deadline=7.0001 严格晚于提交时刻 t=7：任务已完成，信号记为 late
        ledger = ResourceLedger()
        res = run(make_resource_task(ledger), deadline=7.0001)
        o = res.outcome
        self.assertIs(o.kind, Kind.OK)
        self.assertEqual(o.value, {"result": "done", "steps": 4})
        self.assertEqual(o.late, 1)  # 超时信号在清理期间（完成后）到达
        self.assertEqual(ledger.held(), [])


class TestCancelDuringCleanup(unittest.TestCase):
    """清理中再次中断：信号被记录为 deferred，清理不被打断。"""

    def test_second_interrupt_during_cleanup(self):
        ledger = ResourceLedger()
        # t=5 在 WORK 检查点响应超时；清理区间 t=5..7；取消在 t=6.5 到期
        res = run(make_resource_task(ledger), deadline=4.5, cancels=((6.5, "again"),))
        o = res.outcome
        self.assertIs(o.kind, Kind.TIMEOUT)      # 首个信号决定终态
        self.assertEqual(o.phase, "WORK")
        self.assertEqual(o.signal_at, 4.5)
        self.assertEqual(o.responded_at, 5.0)
        self.assertEqual(o.latency, 0.5)
        self.assertEqual(o.deferred, 1)          # 清理期间的信号被记录但未响应
        self.assertEqual(o.released, ("B", "A"))  # 清理完整执行
        self.assertEqual(ledger.held(), [])
        self.assertEqual(res.clock.now, 7.0)     # 清理没有被提前打断


class TestInterruptAfterCompletion(unittest.TestCase):
    """已完成再中断：结果不变（对拍）。"""

    def test_late_interrupt_does_not_change_result(self):
        ledger = ResourceLedger()
        res = run(make_resource_task(ledger))
        before = res.outcome.fingerprint()

        # 完成后立即取消 + 再排一个未来取消 + 再超时式闹钟
        res.token.cancel()
        res.token.cancel(at=res.clock.now + 100.0)  # 第二个迟到信号
        res.clock.advance(100.0)
        res.clock.pump()

        after = res.outcome.fingerprint()
        self.assertEqual(before, after)                 # 对拍：结果指纹不变
        self.assertIs(res.outcome.kind, Kind.OK)
        self.assertEqual(res.outcome.value, {"result": "done", "steps": 4})
        self.assertEqual(res.token.late, 2)             # 两个迟到信号都被观测到
        self.assertEqual(ledger.held(), [])

    def test_deterministic_replay(self):
        # 对拍：同一场景跑两次，结果指纹必须逐位一致
        fps = []
        for _ in range(2):
            ledger = ResourceLedger()
            res = run(make_resource_task(ledger), deadline=4.5, cancels=((6.5, "x"),))
            fps.append(res.outcome.fingerprint())
        self.assertEqual(fps[0], fps[1])


class TestObservableKinds(unittest.TestCase):
    """可观测性：超时 / 主动取消 / 自身失败 三种终态可区分。"""

    def test_cancelled(self):
        ledger = ResourceLedger()
        o = run(make_resource_task(ledger), cancels=((4.5, "user"),)).outcome
        self.assertIs(o.kind, Kind.CANCELLED)
        self.assertEqual(o.phase, "WORK")
        self.assertEqual(o.latency, 0.5)
        self.assertEqual(o.released, ("B", "A"))

    def test_failed(self):
        ledger = ResourceLedger()
        o = run(make_failing_task(ledger)).outcome
        self.assertIs(o.kind, Kind.FAILED)
        self.assertIn("RuntimeError", o.error)
        self.assertEqual(o.phase, "WORK")
        self.assertEqual(o.released, ("A",))  # 失败路径同样完整清理
        self.assertEqual(ledger.held(), [])

    def test_cancel_and_timeout_simultaneous_cancel_wins(self):
        # 同一时刻超时与取消同时到期：策略固定为主动取消优先
        ledger = ResourceLedger()
        o = run(make_resource_task(ledger), deadline=5.0, cancels=((5.0, "user"),)).outcome
        self.assertIs(o.kind, Kind.CANCELLED)

    def test_immediate_timeout_at_entry(self):
        ledger = ResourceLedger()
        o = run(make_resource_task(ledger), deadline=0.0).outcome
        self.assertIs(o.kind, Kind.TIMEOUT)
        self.assertEqual(o.phase, "INIT")
        self.assertEqual(o.latency, 0.0)
        self.assertEqual(o.released, ())
        self.assertEqual(ledger.events, [])  # 什么都没获取，无需清理


class TestNoForcedPreemption(unittest.TestCase):
    """反例：没有检查点的长工作段不会被强制打断。"""

    def test_no_checkpoint_no_preemption(self):
        # 任务在 t=0..10 之间没有任何检查点；deadline=5 只能等到
        # 任务返回后的提交边界检查点（t=10）才被响应
        o = run(make_no_checkpoint_task(), deadline=5.0).outcome
        self.assertIs(o.kind, Kind.TIMEOUT)
        self.assertEqual(o.phase, "SUBMIT")
        self.assertEqual(o.signal_at, 5.0)
        self.assertEqual(o.responded_at, 10.0)
        self.assertEqual(o.latency, 5.0)


class TestCleanupMatrix(unittest.TestCase):
    """中断点落在各个阶段时的清理结果矩阵（含泄漏不变量）。"""

    # deadline, 期望终态, 期望阶段, 期望响应时刻, 期望释放序列
    CASES = [
        (0.0,     Kind.TIMEOUT, "INIT",      0.0, ()),
        (1.0,     Kind.TIMEOUT, "ACQUIRE_A", 1.0, ("A",)),
        (2.0,     Kind.TIMEOUT, "ACQUIRE_B", 3.0, ("B", "A")),
        (3.0,     Kind.TIMEOUT, "ACQUIRE_B", 3.0, ("B", "A")),
        (3.5,     Kind.TIMEOUT, "WORK",      4.0, ("B", "A")),
        (4.5,     Kind.TIMEOUT, "WORK",      5.0, ("B", "A")),
        (6.5,     Kind.TIMEOUT, "SUBMIT",    7.0, ("B", "A")),
        (7.0,     Kind.TIMEOUT, "SUBMIT",    7.0, ("B", "A")),
        (7.0001,  Kind.OK,      "SUBMIT",    None, ("B", "A")),
        (100.0,   Kind.OK,      "SUBMIT",    None, ("B", "A")),
    ]

    def test_matrix(self):
        for deadline, kind, phase, responded_at, released in self.CASES:
            with self.subTest(deadline=deadline):
                ledger = ResourceLedger()
                res = run(make_resource_task(ledger), deadline=deadline)
                o = res.outcome
                self.assertIs(o.kind, kind)
                self.assertEqual(o.phase, phase)
                self.assertEqual(o.responded_at, responded_at)
                self.assertEqual(o.released, released)
                # 不变量 1：任何中断点都不泄漏
                self.assertEqual(ledger.held(), [])
                # 不变量 2：Outcome 报告的释放与台账实际释放一致
                self.assertEqual(tuple(ledger.releases()), o.released)
                # 不变量 3：响应延迟非负，且不超过到下一个检查点的间距
                if o.latency is not None:
                    self.assertGreaterEqual(o.latency, 0.0)
                    self.assertLessEqual(o.latency, 2.0)  # 最长段 ACQUIRE_B(1->3)


if __name__ == "__main__":
    unittest.main()

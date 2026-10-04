"""delayq 自测：python3 test_delayq.py  （时间全部注入，无真实时钟）"""
import os
import tempfile
import unittest

from delayq import DelayQueue, OK, DELIVERED, CANCELED, UNKNOWN


class DelayQTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "queue.log")

    def tearDown(self):
        self.dir.cleanup()

    def open_q(self, **kw):
        return DelayQueue(self.path, **kw)

    # ---- 立即到期 ----
    def test_immediate_due(self):
        q = self.open_q()
        q.schedule("a", 0, "p0")          # 立即到期（at=0）
        q.schedule("b", -100, "p-100")    # 过去时间也算立即到期
        got = q.due(0)
        self.assertEqual([d.id for d in got], ["b", "a"])
        self.assertEqual([d.payload for d in got], ["p-100", "p0"])
        q.close()

    # ---- 远期到期：不到时间不投递 ----
    def test_future_not_due(self):
        q = self.open_q()
        q.schedule("far", 10**12, "x")
        self.assertEqual(q.due(0), [])
        self.assertEqual(q.due(10**12 - 1), [])
        got = q.due(10**12)
        self.assertEqual([d.id for d in got], ["far"])
        q.close()

    # ---- 同一时刻到期：按入队序号确定投递 ----
    def test_same_time_order_by_seq(self):
        q = self.open_q()
        for i in range(50):
            q.schedule(f"m{i}", 1000, f"p{i}")
        got = q.due(1000)
        self.assertEqual([d.id for d in got], [f"m{i}" for i in range(50)])
        self.assertEqual([d.seq for d in got], list(range(50)))
        q.close()

    # ---- 不同时刻：先到期先投递，与入队顺序无关 ----
    def test_due_time_order(self):
        q = self.open_q()
        q.schedule("late", 300, "l")
        q.schedule("early", 100, "e")
        q.schedule("mid", 200, "m")
        got = q.due(300)
        self.assertEqual([d.id for d in got], ["early", "mid", "late"])
        q.close()

    # ---- 取消未到期 ----
    def test_cancel_pending(self):
        q = self.open_q()
        q.schedule("a", 100, "x")
        self.assertEqual(q.cancel("a"), OK)
        self.assertEqual(q.due(1000), [])   # 不再投递
        self.assertEqual(q.cancel("a"), CANCELED)  # 幂等
        q.close()

    # ---- 取消已投递：确定返回 DELIVERED，不回滚不重投 ----
    def test_cancel_after_delivered(self):
        q = self.open_q()
        q.schedule("a", 100, "x")
        got = q.due(100)
        self.assertEqual([d.id for d in got], ["a"])
        self.assertEqual(q.cancel("a"), DELIVERED)
        self.assertEqual(q.cancel("a"), DELIVERED)  # 幂等
        self.assertEqual(q.due(10**9), [])          # 不会重投
        q.close()

    # ---- 取消不存在的 id ----
    def test_cancel_unknown(self):
        q = self.open_q()
        self.assertEqual(q.cancel("nope"), UNKNOWN)
        q.close()

    # ---- 到期后未扫描（堆积）再取消：仍算未投递，取消成功 ----
    def test_cancel_overdue_but_not_scanned(self):
        q = self.open_q()
        q.schedule("a", 100, "x")
        # 时钟已到 500，但还没调用 due()：消息尚未"投递"，取消应成功
        self.assertEqual(q.cancel("a"), OK)
        self.assertEqual(q.due(500), [])
        q.close()

    # ---- 重复 id 拒绝 ----
    def test_duplicate_id_rejected(self):
        q = self.open_q()
        q.schedule("a", 100, "x")
        with self.assertRaises(ValueError):
            q.schedule("a", 200, "y")
        q.close()

    # ---- 重启恢复：顺序与未重启完全一致 ----
    def test_restart_recovers_and_preserves_order(self):
        q = self.open_q()
        for i in range(10):
            q.schedule(f"m{i}", 1000, f"p{i}")
        q.schedule("early", 500, "pe")
        q.cancel("m3")
        q.close()

        q2 = self.open_q()  # 模拟重启
        got = q2.due(1000)
        expect = ["early"] + [f"m{i}" for i in range(10) if i != 3]
        self.assertEqual([d.id for d in got], expect)
        self.assertEqual(q2.cancel("m4"), DELIVERED)  # 重启后取消已投递
        q2.close()

        q3 = self.open_q()  # 再重启：已投递的不重投，已取消的不复活
        self.assertEqual(q3.due(10**9), [])
        self.assertEqual(q3.cancel("m3"), CANCELED)
        self.assertEqual(q3.cancel("m4"), DELIVERED)
        q3.close()

    # ---- 重启后 seq 继续递增，新消息与旧消息同刻到期仍按序 ----
    def test_seq_continues_after_restart(self):
        q = self.open_q()
        q.schedule("old1", 100, "o1")
        q.schedule("old2", 100, "o2")
        q.close()
        q2 = self.open_q()
        q2.schedule("new1", 100, "n1")  # seq 必须排在 old1/old2 之后
        got = q2.due(100)
        self.assertEqual([d.id for d in got], ["old1", "old2", "new1"])
        q2.close()

    # ---- 恢复统计 ----
    def test_recovered_stats(self):
        q = self.open_q()
        q.schedule("a", 1, "x")
        q.schedule("b", 1, "y")
        q.cancel("b")
        q.due(1)
        q.close()
        q2 = self.open_q()
        self.assertEqual(q2.recovered["scheduled"], 2)
        self.assertEqual(q2.stats()["delivered"], 1)
        self.assertEqual(q2.stats()["canceled"], 1)
        q2.close()

    # ---- 空队列 ----
    def test_empty(self):
        q = self.open_q()
        self.assertEqual(q.due(0), [])
        self.assertIsNone(q.peek_due_ms())
        q.close()

    # ---- 大量未到期消息不影响扫描（行为层面验证） ----
    def test_scan_unaffected_by_backlog(self):
        q = self.open_q()
        for i in range(20000):
            q.schedule(f"far{i}", 10**12, "x")
        q.schedule("now", 5, "x")
        got = q.due(5)
        self.assertEqual([d.id for d in got], ["now"])
        q.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

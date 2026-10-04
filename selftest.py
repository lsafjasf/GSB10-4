"""ack_window 自测：按序/乱序/长期不确认/重复确认 + 三种超限策略 + 对拍。

运行：python3 selftest.py
"""

import random
import threading
import time
import unittest

from ack_window import (
    AckWindow,
    DuplicateAckError,
    OverflowPolicy,
    WindowFullError,
)


def ref_replay(pushed_seqs, ack_calls):
    """对拍参考实现：朴素布尔数组，直接标记每个序号的确认状态。"""
    confirmed = {s: False for s in pushed_seqs}
    accepted = 0
    duplicate = 0
    for s in ack_calls:
        if confirmed[s]:
            duplicate += 1
        else:
            confirmed[s] = True
            accepted += 1
    base = 0
    for s in pushed_seqs:
        if confirmed[s]:
            base = s
        else:
            break
    unacked = sum(1 for v in confirmed.values() if not v)
    return base, unacked, accepted, duplicate, confirmed


class OrderingTests(unittest.TestCase):
    def test_in_order(self):
        w = AckWindow(10)
        for s in range(1, 6):
            w.push(s)
        self.assertEqual(w.trace()["unacked"], 5)
        for s in range(1, 6):
            self.assertTrue(w.ack(s))
            self.assertEqual(w.base, s)          # 每个 ACK 都直接推进水位
            self.assertEqual(w.buffered_acks, 0)
        self.assertEqual(w.unacked, 0)
        self.assertEqual(w.acks_accepted, 5)

    def test_out_of_order_advances(self):
        """乱序确认：早到的 ACK 缓存，空洞补齐时水位一次性推进。"""
        w = AckWindow(10)
        for s in range(1, 6):
            w.push(s)
        # 订阅方先回 3,4,5（1,2 未确认，水位不动），再回 2，再回 1
        self.assertTrue(w.ack(3))
        self.assertEqual(w.base, 0)
        self.assertEqual(w.buffered_acks, 1)
        self.assertTrue(w.ack(4))
        self.assertTrue(w.ack(5))
        self.assertEqual((w.base, w.buffered_acks, w.unacked), (0, 3, 2))
        self.assertTrue(w.ack(2))
        # 2 仍没连上水位（缺 1）
        self.assertEqual((w.base, w.buffered_acks, w.unacked), (0, 4, 1))
        self.assertTrue(w.ack(1))
        # 空洞补齐：1->2->3->4->5 全部吸收，水位推进到 5
        self.assertEqual((w.base, w.buffered_acks, w.unacked), (5, 0, 0))

    def test_early_ack_not_mistaken_as_duplicate(self):
        """早到确认不能把后到的当成重复：乱序序列中每个序号都首次生效。"""
        w = AckWindow(10)
        order = [7, 3, 9, 1, 4, 8, 2, 6, 10, 5]
        for s in range(1, 11):
            w.push(s)
        seen = set()
        for s in order:
            self.assertNotIn(s, seen, "测试序内序号唯一")
            seen.add(s)
            self.assertTrue(w.ack(s), "乱序 ACK %d 不应被当成重复" % s)
        self.assertEqual(w.base, 10)
        self.assertEqual(w.acks_accepted, 10)
        self.assertEqual(w.acks_duplicate, 0)

    def test_long_no_ack(self):
        """长时间不确认：窗口一直悬在高位，之后 ACK 到达仍能全部推进。"""
        w = AckWindow(100)
        for s in range(1, 101):
            self.assertTrue(w.push(s))
        self.assertEqual(w.unacked, 100)
        self.assertEqual(w.base, 0)
        time.sleep(0.05)                      # 模拟长时间无 ACK
        self.assertEqual((w.base, w.unacked), (0, 100))
        for s in range(1, 101):
            w.ack(s)
        self.assertEqual((w.base, w.unacked), (100, 0))


class DuplicateTests(unittest.TestCase):
    def test_duplicate_idempotent_with_trace(self):
        """重复确认幂等，未确认计数不得被扣多次；给出计数轨迹。"""
        w = AckWindow(10)
        for s in range(1, 5):
            w.push(s)
        traces = []
        traces.append(("推送 4 条", w.trace()))
        w.ack(1)
        traces.append(("ack 1", w.trace()))
        w.ack(1)                               # 重复：水位之下
        traces.append(("ack 1（重复）", w.trace()))
        w.ack(3)                               # 乱序早到
        traces.append(("ack 3（早到）", w.trace()))
        w.ack(3)                               # 重复：乱序集合内
        traces.append(("ack 3（重复）", w.trace()))
        w.ack(2)                               # 空洞补齐，水位->3
        traces.append(("ack 2", w.trace()))
        w.ack(2)                               # 重复：水位之下
        traces.append(("ack 2（重复）", w.trace()))

        for label, t in traces:
            row = dict(label=label, **t)
            print("%(label)-16s base=%(base)d unacked=%(unacked)d buffered=%(buffered_acks)d "
                  "accepted=%(acks_accepted)d duplicate=%(acks_duplicate)d" % row)

        self.assertEqual(w.acks_accepted, 3)
        self.assertEqual(w.acks_duplicate, 3)
        self.assertEqual(w.unacked, 1)        # 只有 seq=4 未确认，重复 ACK 没多扣
        self.assertEqual(w.base, 3)

    def test_strict_duplicate(self):
        w = AckWindow(10, strict=True)
        w.push(1)
        w.ack(1)
        with self.assertRaises(DuplicateAckError):
            w.ack(1)
        # 抛异常同样不扣计数
        self.assertEqual((w.unacked, w.acks_accepted, w.acks_duplicate), (0, 1, 1))


class OverflowTests(unittest.TestCase):
    def test_pause_policy(self):
        """PAUSE：到达上限时 push 阻塞，ACK 腾出位置后继续。"""
        w = AckWindow(3, policy=OverflowPolicy.PAUSE)
        for s in range(1, 4):
            self.assertTrue(w.push(s))
        pushed = []

        def pusher():
            for s in range(4, 8):
                w.push(s)
                pushed.append(s)

        t = threading.Thread(target=pusher)
        t.start()

        def wait_pushed(n):
            deadline = time.time() + 2
            while len(pushed) < n and time.time() < deadline:
                time.sleep(0.005)
            self.assertEqual(len(pushed), n)

        time.sleep(0.05)
        self.assertEqual(pushed, [])          # 第 4 条 push 被暂停阻塞
        # 每确认 1 条腾出 1 个位置，推送线程随之放行 1 条
        for i, s in enumerate(range(1, 5)):
            w.ack(s)
            wait_pushed(i + 1)
        t.join(timeout=2)
        self.assertEqual(pushed, [4, 5, 6, 7])
        for s in range(5, 8):
            w.ack(s)
        self.assertEqual((w.base, w.unacked), (7, 0))

    def test_drop_new_policy(self):
        """DROP_NEW：满时新消息被丢弃返回 False，旧消息照常确认。"""
        w = AckWindow(2, policy=OverflowPolicy.DROP_NEW)
        self.assertTrue(w.push(1))
        self.assertTrue(w.push(2))
        self.assertFalse(w.push(3))           # 丢弃
        self.assertEqual(w.dropped, 1)
        w.ack(1)
        self.assertTrue(w.push(4))            # 腾出位置后新推送进入
        w.ack(2)
        w.ack(4)
        self.assertEqual((w.base, w.unacked, w.pushed), (4, 0, 3))
        self.assertEqual(w.dropped, 1)

    def test_disconnect_policy(self):
        """DISCONNECT：满时 push 抛 WindowFullError，由调用方断开连接。"""
        w = AckWindow(2, policy=OverflowPolicy.DISCONNECT)
        w.push(1)
        w.push(2)
        with self.assertRaises(WindowFullError):
            w.push(3)
        # 断开重连语义：窗口状态不被超限调用破坏
        self.assertEqual(w.unacked, 2)


class DifferentialTests(unittest.TestCase):
    def test_random_replay_against_reference(self):
        """对拍：随机乱序 + 重复 ACK，窗口终态必须与朴素参考实现一致。"""
        rng = random.Random(20261005)
        for trial in range(200):
            n = rng.randint(1, 200)
            seqs = list(range(1, n + 1))
            w = AckWindow(n)
            for s in seqs:
                w.push(s)
            acks = []
            for s in seqs:                    # 每条至少一次
                acks.append(s)
            for _ in range(n // 3):           # 随机插入重复 ACK
                acks.append(rng.randint(1, n))
            rng.shuffle(acks)
            # 中途抽样对比，再终态对比
            checkpoints = {n, len(acks) // 2, len(acks)}
            for i, s in enumerate(acks, 1):
                w.ack(s)
                if i in checkpoints:
                    rbase, runacked, racc, rdup, _ = ref_replay(seqs, acks[:i])
                    self.assertEqual(w.base, rbase)
                    self.assertEqual(w.unacked, runacked)
                    self.assertEqual(w.acks_accepted, racc)
                    self.assertEqual(w.acks_duplicate, rdup)
            rbase, runacked, racc, rdup, confirmed = ref_replay(seqs, acks)
            self.assertEqual(w.base, n)
            self.assertEqual(w.base, rbase)
            self.assertEqual(w.unacked, 0)
            self.assertEqual(w.unacked, runacked)
            self.assertEqual((w.acks_accepted, w.acks_duplicate), (racc, rdup))

    def test_matches_all_in_order(self):
        """乱序推进结果与全部按序确认结果一致：终态完全相同。"""
        n = 1000
        rng = random.Random(42)
        order = list(range(1, n + 1))
        rng.shuffle(order)

        w_ooo = AckWindow(n)
        for s in range(1, n + 1):
            w_ooo.push(s)
        for s in order:
            w_ooo.ack(s)

        w_seq = AckWindow(n)
        for s in range(1, n + 1):
            w_seq.push(s)
        for s in range(1, n + 1):
            w_seq.ack(s)

        self.assertEqual(w_ooo.trace(), w_seq.trace())
        self.assertEqual(w_ooo.base, n)
        # 对拍数据：两种顺序终态
        print("对拍: 乱序终态 =", w_ooo.trace())
        print("对拍: 按序终态 =", w_seq.trace())


if __name__ == "__main__":
    print("== 重复确认计数轨迹 ==")
    # 先单独跑轨迹用例打印计数轨迹
    suite = unittest.TestLoader().loadTestsFromTestCase(DuplicateTests)
    unittest.TextTestRunner(verbosity=0).run(suite)
    print()
    print("== 全部自测 ==")
    unittest.main(verbosity=2, exit=False)

"""mixed_dispatch 的自测(标准库 unittest)。

运行: python3 -m unittest -v test_mixed_dispatch
"""

import unittest

from mixed_dispatch import Topic
import diff_test


class Mailbox:
    def __init__(self):
        self.messages = []

    def __call__(self, msg):
        self.messages.append(msg)


class BroadcastTests(unittest.TestCase):
    def test_all_broadcast_received_by_everyone(self):
        t = Topic()
        boxes = [Mailbox() for _ in range(5)]
        tokens = [t.subscribe(b) for b in boxes]
        self.assertEqual(t.subscriber_count, 5)
        for i in range(3):
            n = t.publish_broadcast(("B", i))
            self.assertEqual(n, 5)
        for b in boxes:
            self.assertEqual(b.messages, [("B", 0), ("B", 1), ("B", 2)])


class UnicastTests(unittest.TestCase):
    def test_all_unicast_round_robin_sequence(self):
        t = Topic()
        boxes = [Mailbox() for _ in range(4)]
        tokens = [t.subscribe(b) for b in boxes]
        picked = []
        for i in range(10):
            picked.append(t.publish_unicast(("U", i)))
        # 第 k 次单播(k 从 0 计)选择 tokens[k % 4]
        self.assertEqual(picked, [tokens[k % 4] for k in range(10)])
        # 每条消息恰好投递一份
        for i, b in enumerate(boxes):
            self.assertEqual(b.messages,
                             [("U", k) for k in range(10) if k % 4 == i])

    def test_mixed_broadcast_and_unicast(self):
        t = Topic()
        a, b, c = Mailbox(), Mailbox(), Mailbox()
        ta = t.subscribe(a)
        tb = t.subscribe(b)
        tc = t.subscribe(c)

        t.publish_broadcast("b0")          # 所有人
        t.publish_unicast("u0")            # a
        t.publish_unicast("u1")            # b
        t.publish_broadcast("b1")          # 所有人

        self.assertEqual(a.messages, ["b0", "u0", "b1"])
        self.assertEqual(b.messages, ["b0", "u1", "b1"])
        self.assertEqual(c.messages, ["b0", "b1"])


class EmptyTopicTests(unittest.TestCase):
    def test_publish_on_empty_topic_is_noop(self):
        t = Topic()
        self.assertEqual(t.publish_broadcast("x"), 0)
        self.assertIsNone(t.publish_unicast("x"))

    def test_re_subscribe_after_all_unsubscribed(self):
        t = Topic()
        b = Mailbox()
        tok = t.subscribe(b)
        t.publish_unicast("u")
        self.assertTrue(t.unsubscribe(tok))
        self.assertEqual(t.publish_broadcast("b"), 0)
        self.assertIsNone(t.publish_unicast("u"))
        tok2 = t.subscribe(b)
        self.assertEqual(t.publish_unicast("u2"), tok2)
        self.assertEqual(b.messages, ["u", "u2"])


class UnsubscribeTests(unittest.TestCase):
    def test_unsubscribed_receives_nothing(self):
        t = Topic()
        a, b = Mailbox(), Mailbox()
        ta = t.subscribe(a)
        tb = t.subscribe(b)
        t.publish_broadcast("before")
        self.assertTrue(t.unsubscribe(tb))
        self.assertFalse(t.unsubscribe(tb))      # 重复退订: 幂等 False
        t.publish_broadcast("after")
        t.publish_unicast("u")                  # 只剩 a
        self.assertEqual(a.messages, ["before", "after", "u"])
        self.assertEqual(b.messages, ["before"])
        self.assertEqual(t.subscriber_count, 1)

    def test_unicast_sequence_recomputed_after_unsub(self):
        t = Topic()
        boxes = [Mailbox() for _ in range(3)]
        tokens = [t.subscribe(b) for b in boxes]
        for i in range(3):
            t.publish_unicast(i)               # S0 S1 S2 各一次, rr=3
        t.unsubscribe(tokens[1])               # 存活 [S0, S2]
        picked = [t.publish_unicast(10 + i) for i in range(4)]
        # rr=3: 3%2=1 -> S2; 4%2=0 -> S0; 交替
        self.assertEqual(picked,
                         [tokens[2], tokens[0], tokens[2], tokens[0]])


class BoundaryTests(unittest.TestCase):
    """订阅 / 退订边界:每次发布按发布时刻的订阅集合投递,不漏不重。"""

    def test_subscribe_and_unsubscribe_between_publishes(self):
        t = Topic()
        a, b = Mailbox(), Mailbox()
        ta = t.subscribe(a)

        t.publish_broadcast("m1")              # 只有 a
        tb = t.subscribe(b)
        t.publish_broadcast("m2")              # a 和 b
        t.unsubscribe(ta)
        t.publish_broadcast("m3")              # 只有 b

        self.assertEqual(a.messages, ["m1", "m2"])
        self.assertEqual(b.messages, ["m2", "m3"])

    def test_no_duplicate_nor_missing_per_message(self):
        t = Topic()
        boxes = [Mailbox() for _ in range(10)]
        tokens = []
        rounds = []
        for i, b in enumerate(boxes):
            tokens.append(t.subscribe(b))
            n = t.publish_broadcast("after_sub_%d" % i)
            rounds.append(n)
        # 第 i 次广播发生在第 i 个订阅者订阅之后:恰好 i+1 份
        self.assertEqual(rounds, list(range(1, 11)))
        for i, b in enumerate(boxes):
            # S_i 收到它订阅之后的全部广播,共 10-i 条,不多不少
            self.assertEqual(len(b.messages), 10 - i)
            self.assertEqual(b.messages,
                             ["after_sub_%d" % j for j in range(i, 10)])

    def test_snapshot_isolated_from_reentrant_subscribe(self):
        # 发布进行中(处理器里)订阅新订阅者:本次广播不投给它,下次才收到。
        t = Topic()
        late = Mailbox()

        def early(msg):
            if msg == "first":
                t.subscribe(late)              # 重入订阅

        tok = t.subscribe(early)
        n = t.publish_broadcast("first")
        self.assertEqual(n, 1)                 # 快照里只有 early
        self.assertEqual(late.messages, [])
        n = t.publish_broadcast("second")
        self.assertEqual(n, 2)
        self.assertEqual(late.messages, ["second"])

    def test_snapshot_isolated_from_reentrant_unsubscribe(self):
        # 发布进行中退订后续订阅者:本次仍投递(快照),之后才不投递。
        t = Topic()
        later = Mailbox()
        tl = None

        def first(msg):
            if msg == "first":
                t.unsubscribe(tl)              # 重入退订 later

        t.subscribe(first)
        tl = t.subscribe(later)
        n = t.publish_broadcast("first")
        self.assertEqual(n, 2)                 # 快照固定,later 本次仍收到
        self.assertEqual(later.messages, ["first"])
        n = t.publish_broadcast("second")
        self.assertEqual(n, 1)
        self.assertEqual(later.messages, ["first"])


class DifferentialTests(unittest.TestCase):
    def test_matches_manual_delivery_reference_model(self):
        data = diff_test.run_scenario(seed=20261005, n_ops=3000)
        self.assertTrue(data["match"])
        # 对拍确实覆盖到了关键情形
        self.assertGreater(data["stats"]["sub"], 100)
        self.assertGreater(data["stats"]["unsub"], 100)
        self.assertGreater(data["stats"]["broadcast"], 100)
        self.assertGreater(data["stats"]["unicast"], 100)
        self.assertGreater(data["stats"]["unicast_empty"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""自测：覆盖全部广播、全部单播、退订、空订阅者、订阅边界、回调期间变更等。"""

import unittest

from dispatcher import Mode, Topic


def make_recorder(log, sub_id):
    def cb(msg):
        log.append((sub_id, msg.mode.value, msg.seq))
    return cb


class TestBroadcastOnly(unittest.TestCase):
    def test_all_subscribers_receive_exactly_one_copy(self):
        topic = Topic("t")
        log = []
        for name in ["a", "b", "c"]:
            topic.subscribe(name, make_recorder(log, name))
        result = topic.broadcast("x")
        self.assertEqual(result.delivered, ["a", "b", "c"])
        self.assertEqual([entry[0] for entry in log], ["a", "b", "c"])
        self.assertEqual(len(log), 3)  # 每人恰好一份

    def test_broadcast_empty_topic(self):
        topic = Topic("t")
        result = topic.broadcast("x")
        self.assertEqual(result.delivered, [])
        self.assertEqual(result.count, 0)


class TestUnicastOnly(unittest.TestCase):
    def test_round_robin_sequence_reproducible(self):
        topic = Topic("t")
        log = []
        for name in ["a", "b", "c"]:
            topic.subscribe(name, make_recorder(log, name))
        seq = [topic.unicast(i).delivered[0] for i in range(7)]
        self.assertEqual(seq, ["a", "b", "c", "a", "b", "c", "a"])
        # 可复现：同样操作重放一次，序列一致
        topic2 = Topic("t")
        for name in ["a", "b", "c"]:
            topic2.subscribe(name, make_recorder([], name))
        seq2 = [topic2.unicast(i).delivered[0] for i in range(7)]
        self.assertEqual(seq, seq2)

    def test_exactly_one_receiver_per_dispatch(self):
        topic = Topic("t")
        log = []
        for name in ["a", "b"]:
            topic.subscribe(name, make_recorder(log, name))
        for _ in range(10):
            result = topic.unicast()
            self.assertEqual(result.count, 1)
        self.assertEqual(len(log), 10)

    def test_unicast_empty_topic_drops_silently(self):
        topic = Topic("t")
        result = topic.unicast("lost")
        self.assertEqual(result.delivered, [])


class TestUnsubscribe(unittest.TestCase):
    def test_unsubscribed_gets_no_more_messages(self):
        topic = Topic("t")
        log = []
        for name in ["a", "b", "c"]:
            topic.subscribe(name, make_recorder(log, name))
        topic.broadcast()
        self.assertTrue(topic.unsubscribe("b"))
        topic.broadcast()
        receivers = [entry[0] for entry in log]
        self.assertEqual(receivers, ["a", "b", "c", "a", "c"])

    def test_unicast_cursor_after_unsubscribe_before_cursor(self):
        # 游标在 c(2)，退订 a(0) 在游标前 -> 游标前移，下一个仍是 c
        topic = Topic("t")
        for name in ["a", "b", "c"]:
            topic.subscribe(name, make_recorder([], name))
        topic.unicast()  # a
        topic.unicast()  # b
        topic.unsubscribe("a")
        seq = [topic.unicast().delivered[0] for _ in range(4)]
        self.assertEqual(seq, ["c", "b", "c", "b"])

    def test_unicast_cursor_after_unsubscribe_at_cursor(self):
        # 游标在 b(1)，退订 b -> 游标原地指向 c，不跳过 c
        topic = Topic("t")
        for name in ["a", "b", "c"]:
            topic.subscribe(name, make_recorder([], name))
        topic.unicast()  # a
        topic.unsubscribe("b")
        seq = [topic.unicast().delivered[0] for _ in range(4)]
        self.assertEqual(seq, ["c", "a", "c", "a"])

    def test_unsubscribe_all_then_unicast(self):
        topic = Topic("t")
        topic.subscribe("a", make_recorder([], "a"))
        topic.unicast()
        topic.unsubscribe("a")
        self.assertEqual(topic.unicast().delivered, [])
        self.assertEqual(topic.broadcast().delivered, [])

    def test_unsubscribe_unknown_returns_false(self):
        topic = Topic("t")
        self.assertFalse(topic.unsubscribe("ghost"))


class TestSubscriptionBoundary(unittest.TestCase):
    def test_subscribe_boundary_no_miss(self):
        # 边界点之前发起的分发看不到新订阅者，之后必须看到
        topic = Topic("t")
        log = []
        topic.subscribe("a", make_recorder(log, "a"))
        topic.broadcast()  # 边界前
        topic.subscribe("b", make_recorder(log, "b"))
        topic.broadcast()  # 边界后
        receivers = [entry[0] for entry in log]
        self.assertEqual(receivers, ["a", "a", "b"])

    def test_unicast_new_subscriber_joins_round_tail(self):
        topic = Topic("t")
        for name in ["a", "b"]:
            topic.subscribe(name, make_recorder([], name))
        topic.unicast()  # a，游标在 b
        topic.subscribe("c", make_recorder([], "c"))
        seq = [topic.unicast().delivered[0] for _ in range(5)]
        self.assertEqual(seq, ["b", "c", "a", "b", "c"])

    def test_mutation_during_dispatch_affects_next_only(self):
        # 回调执行期间订阅/退订：本次快照不变，下一次生效
        topic = Topic("t")
        log = []

        def cb_a(msg):
            log.append(("a", msg.seq))
            if msg.seq == 1:
                topic.subscribe("c", make_recorder(log, "c"))
                topic.unsubscribe("b")

        topic.subscribe("a", cb_a)
        topic.subscribe("b", make_recorder(log, "b"))
        topic.broadcast()  # 快照 [a, b]：b 仍收到，c 收不到
        topic.broadcast()  # 快照 [a, c]
        receivers = [entry[0] for entry in log]
        self.assertEqual(receivers, ["a", "b", "a", "c"])


class TestMixedModes(unittest.TestCase):
    def test_broadcast_and_unicast_interleaved(self):
        topic = Topic("t")
        log = []
        for name in ["a", "b", "c"]:
            topic.subscribe(name, make_recorder(log, name))
        topic.unicast()      # -> a
        topic.broadcast()    # -> a, b, c
        topic.unicast()      # -> b（广播不消耗轮转）
        receivers = [entry[0] for entry in log]
        self.assertEqual(receivers, ["a", "a", "b", "c", "b"])
        modes = [entry[1] for entry in log]
        self.assertEqual(modes, [Mode.UNICAST.value] + [Mode.BROADCAST.value] * 3 + [Mode.UNICAST.value])


if __name__ == "__main__":
    unittest.main(verbosity=2)

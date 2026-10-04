"""EventBus 隔离与边界用例自测。运行：python3 -m unittest test_pubsub -v"""

import unittest

from pubsub import EventBus, PublishResult, SubscriberError


class TestIsolation(unittest.TestCase):
    def test_single_subscriber_raises_others_still_receive(self):
        bus = EventBus()
        received = []

        def good_a(e):
            received.append(("a", e))

        def bad(e):
            raise ValueError("boom")

        def good_b(e):
            received.append(("b", e))

        id_a = bus.subscribe("t", good_a)
        id_bad = bus.subscribe("t", bad)
        id_b = bus.subscribe("t", good_b)

        result = bus.publish("t", "evt")

        # 其余订阅者照常收到，且顺序未被破坏
        self.assertEqual(received, [("a", "evt"), ("b", "evt")])
        self.assertEqual(result.delivered, [id_a, id_b])
        self.assertFalse(result.ok)
        self.assertEqual(len(result.errors), 1)
        err = result.errors[0]
        self.assertIsInstance(err, SubscriberError)
        self.assertEqual(err.subscription_id, id_bad)
        self.assertIsInstance(err.error, ValueError)   # 原始错误对象
        self.assertEqual(str(err.error), "boom")
        self.assertEqual(err.error_type, "ValueError")
        self.assertIn("boom", err.traceback_str)

    def test_multiple_subscribers_raise_all_reported(self):
        bus = EventBus()
        received = []
        ids = []
        for i in range(5):
            if i in (1, 3):
                def bad(e, i=i):
                    raise RuntimeError(f"fail-{i}")
                ids.append((bus.subscribe("t", bad), True))
            else:
                def good(e, i=i):
                    received.append(i)
                ids.append((bus.subscribe("t", good), False))

        result = bus.publish("t")

        self.assertEqual(received, [0, 2, 4])           # 正常订阅者全部收到
        self.assertEqual(len(result.errors), 2)         # 不只报一条
        self.assertEqual([e.subscription_id for e in result.errors],
                         [ids[1][0], ids[3][0]])        # 按订阅顺序汇总
        self.assertEqual([str(e.error) for e in result.errors], ["fail-1", "fail-3"])
        self.assertEqual(result.delivered, [ids[0][0], ids[2][0], ids[4][0]])

    def test_unsubscribe_during_delivery_takes_effect_immediately(self):
        bus = EventBus()
        calls = []

        def first(e):
            calls.append("first")
            # 投递中取消第三个订阅者
            self.assertTrue(bus.unsubscribe("t", id_third))

        def second(e):
            calls.append("second")

        def third(e):
            calls.append("third")  # 不应被调用

        bus.subscribe("t", first)
        bus.subscribe("t", second)
        id_third = bus.subscribe("t", third)

        result = bus.publish("t")

        self.assertEqual(calls, ["first", "second"])    # 本轮内即生效
        self.assertEqual(result.skipped, [id_third])
        self.assertTrue(result.ok)

    def test_unsubscribe_between_rounds(self):
        bus = EventBus()
        calls = []
        sub_id = bus.subscribe("t", lambda e: calls.append(e))
        bus.publish("t", 1)
        bus.unsubscribe("t", sub_id)
        result = bus.publish("t", 2)
        self.assertEqual(calls, [1])
        self.assertEqual(result.delivered, [])

    def test_no_subscribers(self):
        bus = EventBus()
        result = bus.publish("nothing")
        self.assertIsInstance(result, PublishResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.delivered, [])
        self.assertEqual(result.errors, [])

    def test_delivery_order_is_subscription_order(self):
        bus = EventBus()
        order = []
        for i in range(20):
            bus.subscribe("t", lambda e, i=i: order.append(i))
        bus.publish("t")
        self.assertEqual(order, list(range(20)))

    def test_new_subscriber_during_delivery_not_called_this_round(self):
        bus = EventBus()
        calls = []

        def first(e):
            bus.subscribe("t", lambda e2: calls.append("late"))

        bus.subscribe("t", first)
        bus.publish("t")
        self.assertEqual(calls, [])          # 本轮不调用
        bus.publish("t")
        self.assertEqual(calls, ["late"])    # 下一轮生效

    def test_raise_if_failed_aggregates_all_errors(self):
        bus = EventBus()
        bus.subscribe("t", lambda e: (_ for _ in ()).throw(KeyError("k1")))
        bus.subscribe("t", lambda e: (_ for _ in ()).throw(IndexError("i2")))
        result = bus.publish("t")
        with self.assertRaises(ExceptionGroup) as ctx:
            result.raise_if_failed()
        self.assertEqual(len(ctx.exception.exceptions), 2)

    def test_base_exception_isolated_too(self):
        # 连 KeyboardInterrupt 这类 BaseException 也不应击穿分发循环
        bus = EventBus()
        received = []
        bus.subscribe("t", lambda e: (_ for _ in ()).throw(KeyboardInterrupt()))
        bus.subscribe("t", lambda e: received.append(e))
        result = bus.publish("t", "x")
        self.assertEqual(received, ["x"])
        self.assertEqual(len(result.errors), 1)


if __name__ == "__main__":
    unittest.main()

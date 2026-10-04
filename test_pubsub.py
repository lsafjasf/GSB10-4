"""Self-tests for the isolated in-process pub/sub (stdlib unittest only).

Run:  python3 -m unittest -v test_pubsub
"""

import unittest

from pubsub import (
    AggregatePublishError,
    EventBus,
    SubscriberError,
)


class TestEventBus(unittest.TestCase):
    def test_basic_delivery_in_subscription_order(self):
        bus = EventBus()
        seen = []
        bus.subscribe("t", lambda e: seen.append(("a", e)))
        bus.subscribe("t", lambda e: seen.append(("b", e)))
        bus.subscribe("t", lambda e: seen.append(("c", e)))

        report = bus.publish("t", 42)

        self.assertEqual(seen, [("a", 42), ("b", 42), ("c", 42)])
        self.assertTrue(report.ok)
        self.assertEqual(report.delivered_count, 3)
        self.assertEqual(report.failed_count, 0)

    def test_single_failing_subscriber_is_isolated(self):
        bus = EventBus()
        got = []

        def boom(_):
            raise RuntimeError("kaboom")

        t1 = bus.subscribe("t", lambda e: got.append("a"), name="alpha")
        t2 = bus.subscribe("t", boom, name="boom-handler")
        t3 = bus.subscribe("t", lambda e: got.append("c"), name="gamma")

        report = bus.publish("t", None)

        # Failure of one does not stop later subscribers.
        self.assertEqual(got, ["a", "c"])
        self.assertEqual(report.delivered, [t1, t3])
        self.assertEqual(report.failed_count, 1)

        err = report.errors[0]
        self.assertIsInstance(err, SubscriberError)
        self.assertEqual(err.token, t2)
        self.assertEqual(err.subscriber, "boom-handler")
        self.assertEqual(err.topic, "t")
        self.assertIsInstance(err.error, RuntimeError)
        self.assertEqual(str(err.error), "kaboom")  # original error preserved
        self.assertEqual(err.order, 1)

    def test_multiple_failures_are_all_aggregated(self):
        bus = EventBus()
        got = []

        def fail_a(_):
            raise ValueError("bad value")

        def fail_c(_):
            raise KeyError("missing")

        bus.subscribe("t", fail_a, name="a")
        bus.subscribe("t", lambda e: got.append("b"), name="b")
        bus.subscribe("t", fail_c, name="c")
        bus.subscribe("t", lambda e: got.append("d"), name="d")

        report = bus.publish("t", 1)

        self.assertEqual(got, ["b", "d"])  # healthy ones still run, in order
        self.assertEqual(report.failed_count, 2)
        self.assertEqual([e.subscriber for e in report.errors], ["a", "c"])
        self.assertEqual([e.order for e in report.errors], [0, 2])
        self.assertIsInstance(report.errors[0].error, ValueError)
        self.assertIsInstance(report.errors[1].error, KeyError)
        self.assertIn("ValueError: bad value", report.describe_errors())
        self.assertIn("KeyError: 'missing'", report.describe_errors())

    def test_raise_on_error_exposes_full_aggregate(self):
        bus = EventBus()
        bus.subscribe("t", lambda e: (_ for _ in ()).throw(ValueError("v")))
        bus.subscribe("t", lambda e: (_ for _ in ()).throw(IOError("io")))
        with self.assertRaises(AggregatePublishError) as ctx:
            bus.publish("t", None, raise_on_error=True)
        self.assertEqual(ctx.exception.report.failed_count, 2)
        self.assertEqual(
            [type(e.error).__name__ for e in ctx.exception.report.errors],
            ["ValueError", "OSError"],
        )

    def test_unsubscribe_during_dispatch_takes_effect_this_round(self):
        bus = EventBus()
        called = []
        tokens = {}

        def a(e):
            called.append("a")
            # Cancel a *later* subscriber while dispatch is in progress.
            if tokens["b"] is not None:
                self.assertTrue(bus.unsubscribe(tokens["b"]))
                tokens["b"] = None

        def b(e):
            called.append("b")  # must never run

        def c(e):
            # Cancel itself; nothing should break, and it only runs once.
            called.append("c")
            if tokens["c"] is not None:
                self.assertTrue(bus.unsubscribe(tokens["c"]))
                tokens["c"] = None

        tokens["a"] = bus.subscribe("t", a, name="a")
        tokens["b"] = bus.subscribe("t", b, name="b")
        tokens["c"] = bus.subscribe("t", c, name="c")
        token_a, token_c = tokens["a"], tokens["c"]

        report = bus.publish("t", None)

        self.assertEqual(called, ["a", "c"])
        self.assertEqual(report.delivered, [token_a, token_c])
        self.assertEqual(report.skipped_unsubscribed, [2])
        self.assertTrue(report.ok)

        # A second publish confirms both unsubscriptions are durable.
        called.clear()
        report2 = bus.publish("t", None)
        self.assertEqual(called, ["a"])
        self.assertEqual(report2.delivered, [token_a])

    def test_unsubscribe_before_publish_never_called(self):
        bus = EventBus()
        called = []
        t1 = bus.subscribe("t", lambda e: called.append("a"))
        t2 = bus.subscribe("t", lambda e: called.append("b"))
        self.assertTrue(bus.unsubscribe(t2))
        self.assertFalse(bus.unsubscribe(t2))  # already gone

        report = bus.publish("t", None)
        self.assertEqual(called, ["a"])
        self.assertEqual(report.delivered, [t1])
        self.assertEqual(bus.subscriber_count("t"), 1)

    def test_failing_then_unsubscribe_next_round(self):
        bus = EventBus()
        calls = []

        def flaky(e):
            calls.append("flaky")
            raise RuntimeError("still failing")

        t = bus.subscribe("t", flaky)
        bus.subscribe("t", lambda e: calls.append("ok"))
        first = bus.publish("t")
        self.assertEqual(first.failed_count, 1)
        self.assertEqual(calls, ["flaky", "ok"])

        bus.unsubscribe(t)
        second = bus.publish("t")
        self.assertTrue(second.ok)
        self.assertEqual(calls, ["flaky", "ok", "ok"])

    def test_no_subscribers_is_successful_empty_delivery(self):
        bus = EventBus()
        report = bus.publish("never-published", {"x": 1})
        self.assertTrue(report.ok)
        self.assertEqual(report.delivered, [])
        self.assertEqual(report.errors, [])
        self.assertEqual(report.delivered_count, 0)

    def test_topics_are_isolated(self):
        bus = EventBus()
        seen_a, seen_b = [], []
        bus.subscribe("a", lambda e: seen_a.append(e))
        bus.subscribe("b", lambda e: seen_b.append(e))
        report = bus.publish("a", "hello")
        self.assertEqual(seen_a, ["hello"])
        self.assertEqual(seen_b, [])
        self.assertEqual(report.delivered_count, 1)

    def test_subscribe_during_dispatch_not_invoked_this_round(self):
        bus = EventBus()
        late_calls = []

        def early(e):
            bus.subscribe("t", lambda ev: late_calls.append(ev), name="late")

        bus.subscribe("t", early, name="early")
        report = bus.publish("t", "e1")
        self.assertEqual(late_calls, [])
        self.assertEqual(report.delivered_count, 1)
        # It participates from the next publish onwards, at the end.
        report2 = bus.publish("t", "e2")
        self.assertEqual(late_calls, ["e2"])
        self.assertEqual(report2.delivered_count, 2)

    def test_dispatch_remains_serial_under_threads(self):
        # Many threads publishing concurrently must still produce
        # deterministic per-publish ordering (subscription order each time).
        import threading

        bus = EventBus()
        order = []
        for i in range(5):
            bus.subscribe("t", lambda e, i=i: order.append((e, i)))

        def worker(start):
            for e in range(start, start + 20):
                bus.publish("t", e)

        threads = [threading.Thread(target=worker, args=(i * 20,))
                   for i in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        # Every publish delivered to all 5 subscribers in order 0..4.
        self.assertEqual(len(order), 80 * 5)
        for i in range(0, len(order), 5):
            event = order[i][0]
            self.assertEqual(order[i:i + 5], [(event, 0), (event, 1),
                                              (event, 2), (event, 3),
                                              (event, 4)])


if __name__ == "__main__":
    unittest.main(verbosity=2)

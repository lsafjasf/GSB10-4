"""Self-tests for the lazyinit package (standard library unittest only).

Run:  python3 -m unittest discover -s tests -v
"""

import threading
import time
import unittest

from lazyinit import (
    FailurePolicy,
    InitializationFailedError,
    Lazy,
    LazyLocked,
    RecursiveInitializationError,
)

IMPLEMENTATIONS = [("Lazy", Lazy), ("LazyLocked", LazyLocked)]

THREADS = 16
ROUNDS = 200


def _run_concurrent_gets(impl, threads=THREADS):
    """Fire `threads` threads at get() simultaneously; return results."""
    barrier = threading.Barrier(threads)
    results = [None] * threads
    errors = []

    def worker(i):
        try:
            barrier.wait(timeout=10)
            results[i] = impl.get()
        except Exception as exc:  # pragma: no cover - diagnostic
            errors.append(exc)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert not errors, errors
    return results


class TestSingleThread(unittest.TestCase):
    def test_single_thread_basic(self):
        for name, cls in IMPLEMENTATIONS:
            with self.subTest(impl=name):
                calls = []
                lazy = cls(lambda: calls.append(1) or {"k": "v"})
                self.assertEqual(lazy.state, "unset")
                v1 = lazy.get()
                v2 = lazy.get()
                self.assertIs(v1, v2)
                self.assertEqual(v1, {"k": "v"})
                self.assertEqual(len(calls), 1)
                self.assertEqual(lazy.stats, {"attempts": 1, "successes": 1, "failures": 0})
                self.assertEqual(lazy.state, "ready")
                self.assertTrue(lazy.is_initialized())

    def test_value_none_and_falsy(self):
        for name, cls in IMPLEMENTATIONS:
            with self.subTest(impl=name):
                lazy = cls(lambda: None)
                self.assertIsNone(lazy.get())
                self.assertEqual(lazy.stats["successes"], 1)


class TestConcurrentFirstAccess(unittest.TestCase):
    def test_exactly_one_creation_under_race(self):
        for name, cls in IMPLEMENTATIONS:
            with self.subTest(impl=name):
                creations = 0
                counter_lock = threading.Lock()

                def factory():
                    nonlocal creations
                    with counter_lock:
                        creations += 1
                    time.sleep(0.001)  # widen the race window
                    return object()

                for _ in range(ROUNDS):
                    lazy = cls(factory)
                    results = _run_concurrent_gets(lazy)
                    self.assertEqual(creations, 1, f"created {creations} times")
                    self.assertTrue(all(r is results[0] for r in results))
                    self.assertEqual(lazy.stats["attempts"], 1)
                    creations = 0


class TestVisibility(unittest.TestCase):
    """No thread may observe a partially constructed object."""

    def test_no_partial_object_visible(self):
        for name, cls in IMPLEMENTATIONS:
            with self.subTest(impl=name):
                init_started = threading.Event()
                finish_init = threading.Event()
                observed = []

                class Widget:
                    def __init__(self):
                        self.stage = 0
                        init_started.set()
                        # Hold construction open while readers hammer get().
                        deadline = time.time() + 0.3
                        while time.time() < deadline and not finish_init.is_set():
                            time.sleep(0.001)
                        self.stage = 1
                        self.stage = 2
                        self.ready = True

                lazy = cls(Widget)
                init_thread = threading.Thread(target=lazy.get)
                init_thread.start()
                self.assertTrue(init_started.wait(timeout=5))

                stop = threading.Event()

                def reader():
                    while not stop.is_set():
                        w = lazy.get()  # blocks until publication
                        observed.append(w.stage)
                        if not getattr(w, "ready", False):
                            observed.append("NOT_READY")

                readers = [threading.Thread(target=reader) for _ in range(8)]
                for r in readers:
                    r.start()
                time.sleep(0.2)
                finish_init.set()
                init_thread.join(timeout=10)
                stop.set()
                for r in readers:
                    r.join(timeout=10)

                self.assertTrue(observed, "readers never returned")
                self.assertTrue(
                    all(s == 2 for s in observed),
                    f"partial object observed: {observed[:10]}",
                )

    def test_broken_publish_then_fill_is_observable(self):
        """Negative control: publish the reference *before* filling fields.

        This mirrors the classic broken pattern and shows that the hazard
        our implementation avoids is real and observable.
        """
        holder = {}
        published = threading.Event()
        fill = threading.Event()

        class Broken:
            def __init__(self):
                holder["obj"] = self  # publish first (the bug)
                published.set()
                fill.wait(timeout=5)
                self.ready = True

        t = threading.Thread(target=Broken)
        t.start()
        self.assertTrue(published.wait(timeout=5))
        # Another thread can see the object before its fields exist.
        self.assertFalse(hasattr(holder["obj"], "ready"))
        fill.set()
        t.join(timeout=10)
        self.assertTrue(holder["obj"].ready)


class TestFailureSemantics(unittest.TestCase):
    def test_retry_policy_allows_retry(self):
        for name, cls in IMPLEMENTATIONS:
            with self.subTest(impl=name):
                attempts = []

                def factory():
                    attempts.append(1)
                    if len(attempts) < 3:
                        raise RuntimeError(f"boom {len(attempts)}")
                    return "ok"

                lazy = cls(factory, failure_policy=FailurePolicy.RETRY)
                for _ in range(2):
                    with self.assertRaises(InitializationFailedError) as ctx:
                        lazy.get()
                    self.assertIsInstance(ctx.exception.__cause__, RuntimeError)
                self.assertEqual(lazy.get(), "ok")
                self.assertEqual(
                    lazy.stats, {"attempts": 3, "successes": 1, "failures": 2}
                )

    def test_permanent_policy_latches_failure(self):
        for name, cls in IMPLEMENTATIONS:
            with self.subTest(impl=name):
                attempts = []

                def factory():
                    attempts.append(1)
                    raise ValueError("nope")

                lazy = cls(factory, failure_policy=FailurePolicy.PERMANENT)
                for _ in range(5):
                    with self.assertRaises(InitializationFailedError) as ctx:
                        lazy.get()
                    self.assertIsInstance(ctx.exception.__cause__, ValueError)
                # Factory ran exactly once; failure is latched.
                self.assertEqual(len(attempts), 1)
                self.assertEqual(lazy.state, "failed")
                # Explicit reset makes it retryable again.
                lazy.reset()
                self.assertEqual(lazy.state, "unset")
                with self.assertRaises(InitializationFailedError):
                    lazy.get()
                self.assertEqual(len(attempts), 2)

    def test_concurrent_failure_retry_eventually_succeeds(self):
        for name, cls in IMPLEMENTATIONS:
            with self.subTest(impl=name):
                calls = []
                lock = threading.Lock()

                def factory():
                    with lock:
                        calls.append(1)
                        n = len(calls)
                    if n == 1:
                        raise RuntimeError("first attempt fails")
                    return n

                lazy = cls(factory, failure_policy=FailurePolicy.RETRY)
                # First wave: all threads fail together (one attempt).
                with self.assertRaises(InitializationFailedError):
                    lazy.get()
                # Second wave: concurrent retry succeeds exactly once.
                results = _run_concurrent_gets(lazy)
                self.assertTrue(all(r == results[0] for r in results))
                self.assertEqual(lazy.stats["successes"], 1)


class TestRecursiveAccess(unittest.TestCase):
    def test_recursion_detected_same_thread(self):
        for name, cls in IMPLEMENTATIONS:
            with self.subTest(impl=name):
                lazy = cls(lambda: lazy.get())  # noqa: F821 - self reference
                with self.assertRaises(RecursiveInitializationError):
                    lazy.get()
                # RETRY default: state is clean afterwards, can replace factory.
                self.assertEqual(lazy.state, "unset")

    def test_recursion_error_unwraps_to_caller(self):
        for name, cls in IMPLEMENTATIONS:
            with self.subTest(impl=name):
                lazy = cls(lambda: lazy.get())  # noqa: F821
                try:
                    lazy.get()
                except RecursiveInitializationError:
                    pass  # raised directly, not wrapped
                else:
                    self.fail("expected RecursiveInitializationError")

    def test_other_threads_block_during_initialization(self):
        for name, cls in IMPLEMENTATIONS:
            with self.subTest(impl=name):
                entered = threading.Event()
                release = threading.Event()

                def factory():
                    entered.set()
                    release.wait(timeout=5)
                    return 42

                lazy = cls(factory)
                t1 = threading.Thread(target=lazy.get)
                t1.start()
                self.assertTrue(entered.wait(timeout=5))
                got = []

                def reader():
                    got.append(lazy.get())

                t2 = threading.Thread(target=reader)
                t2.start()
                time.sleep(0.1)
                self.assertEqual(got, [], "reader returned before init finished")
                release.set()
                t1.join(timeout=10)
                t2.join(timeout=10)
                self.assertEqual(got, [42])


class TestReset(unittest.TestCase):
    def test_reset_allows_reinit(self):
        for name, cls in IMPLEMENTATIONS:
            with self.subTest(impl=name):
                values = iter(["a", "b"])
                lazy = cls(lambda: next(values))
                self.assertEqual(lazy.get(), "a")
                lazy.reset()
                self.assertEqual(lazy.get(), "b")
                self.assertEqual(lazy.stats["successes"], 2)


if __name__ == "__main__":
    unittest.main()

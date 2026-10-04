"""lazy.py 的自测（unittest，仅标准库）。

覆盖：单线程、多线程同时首次访问（创建次数统计）、构造抛错
（可重试 / 永久失败两种策略）、初始化期间递归访问、
内存可见性（读不到半成品 / 构造完成前读不到）。
"""

import threading
import time
import unittest

from lazy import Lazy, LockedLazy

THREADS = 32


def run_concurrently(fn, n=THREADS):
    """n 个线程在同一道栅栏后同时出发执行 fn，返回结果列表。"""
    barrier = threading.Barrier(n)
    results = [None] * n
    errors = []

    def worker(i):
        try:
            barrier.wait(timeout=10)
            results[i] = fn()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "worker thread stuck"
    if errors:
        raise errors[0]
    return results


class TestSingleThread(unittest.TestCase):
    def test_single_thread(self):
        calls = []
        lazy = Lazy(lambda: calls.append(1) or {"k": "v"})
        self.assertEqual(lazy.get(), {"k": "v"})
        again = lazy.get()
        self.assertEqual(calls, [1], "factory must run exactly once")
        self.assertIs(again, lazy.get(), "must return the same object")


class TestConcurrentFirstAccess(unittest.TestCase):
    def test_created_exactly_once(self):
        for cls in (Lazy, LockedLazy):
            with self.subTest(impl=cls.__name__):
                calls = []
                calls_lock = threading.Lock()

                def factory():
                    time.sleep(0.01)  # 放大竞争窗口
                    with calls_lock:
                        calls.append(1)
                    return object()

                lazy = cls(factory)
                results = run_concurrently(lazy.get)
                self.assertEqual(len(calls), 1,
                                 f"{cls.__name__}: factory ran {len(calls)} times")
                self.assertTrue(all(r is results[0] for r in results),
                                "all threads must get the identical object")


class TestFailureSemantics(unittest.TestCase):
    def test_retryable_by_default(self):
        calls = []

        def factory():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("boom")
            return "ok"

        lazy = Lazy(factory)  # cache_exceptions=False（默认）
        with self.assertRaises(ValueError):
            lazy.get()
        self.assertEqual(lazy.get(), "ok", "transient failure must be retryable")
        self.assertEqual(len(calls), 2)
        self.assertEqual(lazy.get(), "ok")
        self.assertEqual(len(calls), 2, "no recompute after success")

    def test_permanent_failure(self):
        calls = []

        def factory():
            calls.append(1)
            raise ValueError("permanent")

        lazy = Lazy(factory, cache_exceptions=True)
        with self.assertRaises(ValueError) as first:
            lazy.get()
        with self.assertRaises(ValueError) as second:
            lazy.get()
        self.assertIs(first.exception, second.exception,
                      "cached exception must be re-raised")
        self.assertEqual(len(calls), 1, "factory must not run again")

    def test_concurrent_failure_then_retry(self):
        calls = []
        calls_lock = threading.Lock()

        def factory():
            with calls_lock:
                calls.append(1)
                n = len(calls)
            if n == 1:
                raise RuntimeError("first attempt fails")
            return "recovered"

        lazy = Lazy(factory)
        # 可重试语义下，并发调用者可能看到第一次失败，
        # 也可能看到随后某次重试的成功结果——两者都合法。
        def try_get():
            try:
                return ("ok", lazy.get())
            except RuntimeError as exc:
                return ("err", str(exc))

        results = run_concurrently(try_get)
        allowed = {("err", "first attempt fails"), ("ok", "recovered")}
        self.assertTrue(all(r in allowed for r in results))
        self.assertIn(("err", "first attempt fails"), results,
                      "at least one thread must observe the failure")
        # 最终状态收敛为成功值，工厂调用次数 >= 2（失败 1 次 + 成功 1 次）
        self.assertEqual(lazy.get(), "recovered")
        self.assertGreaterEqual(len(calls), 2)


class TestRecursiveAccess(unittest.TestCase):
    def test_recursion_raises_instead_of_deadlock(self):
        holder = {}
        calls = []

        def factory():
            calls.append(1)
            return holder["lazy"].get()  # 初始化期间递归访问

        lazy = Lazy(factory)
        holder["lazy"] = lazy
        with self.assertRaises(RuntimeError) as ctx:
            lazy.get()
        self.assertIn("recursive", str(ctx.exception))
        # 递归错误不应污染状态：之后仍可正常初始化
        holder["lazy"] = None
        lazy2 = Lazy(lambda: "fine")
        self.assertEqual(lazy2.get(), "fine")

    def test_recursion_does_not_corrupt_state(self):
        holder = {}

        def factory():
            try:
                holder["lazy"].get()
            except RuntimeError:
                pass
            return "built"

        lazy = Lazy(factory)
        holder["lazy"] = lazy
        self.assertEqual(lazy.get(), "built")
        self.assertEqual(lazy.get(), "built")


class TestVisibility(unittest.TestCase):
    def test_no_partial_object_visible(self):
        """工厂带延迟地构造大对象；所有读者必须看到完整对象。"""
        FIELDS = 2000
        calls = []

        def factory():
            calls.append(1)
            data = {}
            for i in range(FIELDS):
                data[f"field_{i}"] = i
                if i == FIELDS // 2:
                    time.sleep(0.02)  # 构造中途放大竞争窗口
            data["checksum"] = sum(range(FIELDS))
            return data

        lazy = Lazy(factory)
        results = run_concurrently(lazy.get, n=16)
        expected_checksum = sum(range(FIELDS))
        for obj in results:
            self.assertEqual(len(obj), FIELDS + 1)
            self.assertEqual(obj["checksum"], expected_checksum)
            for i in range(FIELDS):
                self.assertEqual(obj[f"field_{i}"], i,
                                 "reader observed a half-built object")
        self.assertEqual(len(calls), 1)

    def test_not_readable_before_construction_completes(self):
        """构造未完成前，其他线程的 get() 必须阻塞，读不到任何值。"""
        may_finish = threading.Event()
        lazy = Lazy(lambda: (may_finish.wait(10), "done")[1])

        initializer = threading.Thread(target=lazy.get)
        initializer.start()
        time.sleep(0.1)  # 确保工厂已在执行、锁已被持有

        reader_done = threading.Event()
        reader_result = []

        def reader():
            reader_result.append(lazy.get())
            reader_done.set()

        r = threading.Thread(target=reader)
        r.start()
        time.sleep(0.3)
        self.assertFalse(reader_done.is_set(),
                         "reader must block while construction is in progress")
        self.assertEqual(reader_result, [], "no value readable before completion")

        may_finish.set()
        initializer.join(timeout=10)
        r.join(timeout=10)
        self.assertEqual(reader_result, ["done"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Self-tests for the idempotency-key library.  Standard library only.

Run:  python3 -m unittest discover -s tests -v   (from the idempotency/ dir)
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from idempotency import (
    DEFAULT_TTL_SECONDS,
    IdempotencyStore,
    InProgressError,
    InProgressPolicy,
    InvalidKeyError,
    KeyConflictError,
    KeyExpiredError,
    Response,
    WaitTimeoutError,
    request_fingerprint,
    validate_key,
)

USER = "user-123"
ENDPOINT = "POST /orders"
KEY = "ord_9f2c7e1a-0001"
BODY = b'{"sku":"A-1","qty":2}'


def fp(body=BODY, method="POST", path="/orders"):
    return request_fingerprint(method, path, body)


def ok_handler_factory(counter, status=201, body=b'{"order_id":"O-1"}', delay=0.0):
    def handler():
        counter["n"] += 1
        if delay:
            time.sleep(delay)
        return Response(status=status, body=body, headers=(("Content-Type", "application/json"),))
    return handler


class FakeClock:
    def __init__(self, t=1_000.0):
        self.t = t
    def __call__(self):
        return self.t
    def advance(self, dt):
        self.t += dt


class TestKeyValidation(unittest.TestCase):
    def test_valid_keys_accepted(self):
        for k in ("a" * 8, "k" * 128, "Abc_09-xYz", "ord_9f2c7e1a"):
            self.assertEqual(validate_key(k), k)

    def test_invalid_keys_rejected_with_400(self):
        bad = [
            "", "short", "k" * 129, "has space", "has.dot", "has/slash",
            "中文键值测试", "tab\tkey", None, 12345, b"bytes-not-str",
        ]
        for k in bad:
            with self.assertRaises(InvalidKeyError) as ctx:
                validate_key(k)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_boundary_lengths(self):
        validate_key("x" * 8)
        validate_key("x" * 128)
        for k in ("x" * 7, "x" * 129):
            with self.assertRaises(InvalidKeyError):
                validate_key(k)


class TestFirstAndReplay(unittest.TestCase):
    def setUp(self):
        self.store = IdempotencyStore()
        self.counter = {"n": 0}

    def test_first_request_executes_handler(self):
        resp, replayed = self.store.execute(
            USER, ENDPOINT, KEY, fp(), ok_handler_factory(self.counter))
        self.assertFalse(replayed)
        self.assertEqual(resp.status, 201)
        self.assertEqual(self.counter["n"], 1)

    def test_replay_returns_identical_status_and_body(self):
        h = ok_handler_factory(self.counter)
        r1, _ = self.store.execute(USER, ENDPOINT, KEY, fp(), h)
        r2, replayed2 = self.store.execute(USER, ENDPOINT, KEY, fp(), h)
        r3, replayed3 = self.store.execute(USER, ENDPOINT, KEY, fp(), h)
        self.assertTrue(replayed2 and replayed3)
        self.assertEqual(self.counter["n"], 1, "handler must run exactly once")
        self.assertEqual((r1.status, r1.body), (r2.status, r2.body))
        self.assertEqual((r1.status, r1.body), (r3.status, r3.body))
        self.assertEqual(r1.headers, r2.headers)

    def test_same_key_different_user_isolated(self):
        c2 = {"n": 0}
        self.store.execute("user-A", ENDPOINT, KEY, fp(), ok_handler_factory(self.counter))
        self.store.execute("user-B", ENDPOINT, KEY, fp(), ok_handler_factory(c2))
        self.assertEqual(self.counter["n"], 1)
        self.assertEqual(c2["n"], 1, "different user must not hit user-A's record")

    def test_same_key_different_endpoint_isolated(self):
        c2 = {"n": 0}
        self.store.execute(USER, "POST /orders", KEY, fp(), ok_handler_factory(self.counter))
        self.store.execute(USER, "POST /refunds", KEY, fp(), ok_handler_factory(c2))
        self.assertEqual(self.counter["n"], 1)
        self.assertEqual(c2["n"], 1, "different endpoint must not share the record")

    def test_scope_not_flattenable(self):
        # ("ab","c") vs ("a","bc") would collide under naive string concat.
        c1, c2 = {"n": 0}, {"n": 0}
        self.store.execute("ab", "c", KEY, fp(), ok_handler_factory(c1))
        self.store.execute("a", "bc", KEY, fp(), ok_handler_factory(c2))
        self.assertEqual((c1["n"], c2["n"]), (1, 1))

    def test_conflict_same_key_different_body(self):
        self.store.execute(USER, ENDPOINT, KEY, fp(BODY), ok_handler_factory(self.counter))
        with self.assertRaises(KeyConflictError) as ctx:
            self.store.execute(USER, ENDPOINT, KEY, fp(b'{"sku":"A-1","qty":3}'),
                               ok_handler_factory(self.counter))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.counter["n"], 1)

    def test_conflict_same_body_different_method(self):
        self.store.execute(USER, ENDPOINT, KEY, fp(BODY, "POST", "/orders"),
                           ok_handler_factory(self.counter))
        with self.assertRaises(KeyConflictError):
            self.store.execute(USER, ENDPOINT, KEY, fp(BODY, "PUT", "/orders"),
                               ok_handler_factory(self.counter))

    def test_handler_exception_evicts_record_retry_is_fresh(self):
        def boom():
            self.counter["n"] += 1
            raise RuntimeError("db down")
        with self.assertRaises(RuntimeError):
            self.store.execute(USER, ENDPOINT, KEY, fp(), boom)
        resp, replayed = self.store.execute(
            USER, ENDPOINT, KEY, fp(), ok_handler_factory(self.counter))
        self.assertFalse(replayed, "after a crash the retry must be a fresh first request")
        self.assertEqual(resp.status, 201)

    def test_invalid_key_never_touches_state(self):
        with self.assertRaises(InvalidKeyError):
            self.store.execute(USER, ENDPOINT, "bad key!", fp(),
                               ok_handler_factory(self.counter))
        self.assertEqual(len(self.store), 0)
        self.assertEqual(self.counter["n"], 0)


class TestExpiry(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.store = IdempotencyStore(ttl_seconds=60, clock=self.clock)
        self.counter = {"n": 0}

    def test_replay_before_ttl_ok_after_ttl_410(self):
        self.store.execute(USER, ENDPOINT, KEY, fp(), ok_handler_factory(self.counter))
        self.clock.advance(59)
        _, replayed = self.store.execute(USER, ENDPOINT, KEY, fp(),
                                         ok_handler_factory(self.counter))
        self.assertTrue(replayed)
        self.clock.advance(2)  # now past TTL
        with self.assertRaises(KeyExpiredError) as ctx:
            self.store.execute(USER, ENDPOINT, KEY, fp(), ok_handler_factory(self.counter))
        self.assertEqual(ctx.exception.status_code, 410)

    def test_expired_replay_is_never_treated_as_first_request(self):
        self.store.execute(USER, ENDPOINT, KEY, fp(), ok_handler_factory(self.counter))
        self.clock.advance(3600)
        for _ in range(3):
            with self.assertRaises(KeyExpiredError):
                self.store.execute(USER, ENDPOINT, KEY, fp(),
                                   ok_handler_factory(self.counter))
        self.assertEqual(self.counter["n"], 1, "handler must never re-run for an expired key")

    def test_expired_beats_conflict(self):
        # Expired key replayed with a *different* body: 410, not 409.
        self.store.execute(USER, ENDPOINT, KEY, fp(BODY), ok_handler_factory(self.counter))
        self.clock.advance(3600)
        with self.assertRaises(KeyExpiredError):
            self.store.execute(USER, ENDPOINT, KEY, fp(b"other"), ok_handler_factory(self.counter))

    def test_expiry_is_terminal(self):
        self.store.execute(USER, ENDPOINT, KEY, fp(), ok_handler_factory(self.counter))
        self.clock.advance(3600)
        with self.assertRaises(KeyExpiredError):
            self.store.execute(USER, ENDPOINT, KEY, fp(), ok_handler_factory(self.counter))
        # Clock "moves back" (NTP correction): still expired, never completed again.
        self.clock.advance(-3600)
        with self.assertRaises(KeyExpiredError):
            self.store.execute(USER, ENDPOINT, KEY, fp(), ok_handler_factory(self.counter))

    def test_sweep_removes_expired_records(self):
        for i in range(5):
            self.store.execute(USER, ENDPOINT, f"key-{i:08d}", fp(),
                               ok_handler_factory(self.counter))
        self.assertEqual(len(self.store), 5)
        self.clock.advance(3600)
        self.assertEqual(self.store.sweep(), 5)
        self.assertEqual(len(self.store), 0)


class TestConcurrency(unittest.TestCase):
    def test_concurrent_duplicates_wait_and_get_identical_response(self):
        store = IdempotencyStore(in_progress_policy=InProgressPolicy.WAIT)
        counter = {"n": 0}
        handler = ok_handler_factory(counter, delay=0.3)
        results, errors = [], []

        def worker():
            try:
                results.append(store.execute(USER, ENDPOINT, KEY, fp(), handler))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(counter["n"], 1, "handler must run exactly once")
        self.assertEqual(len(results), 16)
        first_status, first_body = results[0][0].status, results[0][0].body
        for resp, _ in results:
            self.assertEqual((resp.status, resp.body), (first_status, first_body))
        self.assertEqual(sum(1 for _, r in results if r), 15, "exactly one non-replay")

    def test_reject_policy_answers_409_while_in_flight(self):
        store = IdempotencyStore(in_progress_policy=InProgressPolicy.REJECT)
        counter = {"n": 0}
        started = threading.Event()
        release = threading.Event()

        def slow():
            counter["n"] += 1
            started.set()
            release.wait(5)
            return Response(status=201, body=b"{}")

        t = threading.Thread(target=lambda: store.execute(USER, ENDPOINT, KEY, fp(), slow))
        t.start()
        started.wait(5)
        with self.assertRaises(InProgressError) as ctx:
            store.execute(USER, ENDPOINT, KEY, fp(), ok_handler_factory({"n": 0}))
        self.assertEqual(ctx.exception.status_code, 409)
        release.set()
        t.join()
        # After completion the same key replays normally.
        resp, replayed = store.execute(USER, ENDPOINT, KEY, fp(), ok_handler_factory({"n": 0}))
        self.assertTrue(replayed)
        self.assertEqual(resp.status, 201)

    def test_in_progress_policy_answers_202(self):
        store = IdempotencyStore(in_progress_policy=InProgressPolicy.IN_PROGRESS)
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(5)
            return Response(status=201, body=b"{}")

        t = threading.Thread(target=lambda: store.execute(USER, ENDPOINT, KEY, fp(), slow))
        t.start()
        started.wait(5)
        resp, replayed = store.execute(USER, ENDPOINT, KEY, fp(), ok_handler_factory({"n": 0}))
        self.assertTrue(replayed)
        self.assertEqual(resp.status, 202)
        release.set()
        t.join()

    def test_wait_timeout(self):
        store = IdempotencyStore(in_progress_policy=InProgressPolicy.WAIT,
                                 wait_timeout_seconds=0.2)
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(5)
            return Response(status=201, body=b"{}")

        t = threading.Thread(target=lambda: store.execute(USER, ENDPOINT, KEY, fp(), slow))
        t.start()
        started.wait(5)
        with self.assertRaises(WaitTimeoutError) as ctx:
            store.execute(USER, ENDPOINT, KEY, fp(), ok_handler_factory({"n": 0}))
        self.assertEqual(ctx.exception.status_code, 409)
        release.set()
        t.join()

    def test_waiter_after_handler_crash_becomes_fresh_first(self):
        store = IdempotencyStore(in_progress_policy=InProgressPolicy.WAIT)
        started = threading.Event()
        outcome = {}

        def boom():
            started.set()
            time.sleep(0.2)
            raise RuntimeError("db down")

        def first():
            try:
                store.execute(USER, ENDPOINT, KEY, fp(), boom)
            except RuntimeError:
                outcome["first"] = "raised"

        def waiter():
            started.wait(5)
            time.sleep(0.02)  # first() has claimed the key by now
            resp, replayed = store.execute(
                USER, ENDPOINT, KEY, fp(),
                ok_handler_factory({"n": 0}, status=201, body=b"ok"))
            outcome["waiter"] = (resp.status, resp.body, replayed)

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=waiter)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(outcome.get("first"), "raised")
        # The crashed record was evicted, so the blocked waiter re-claimed
        # the key and executed as a fresh first request.
        self.assertEqual(outcome.get("waiter"), (201, b"ok", False))

    def test_many_keys_many_threads_no_cross_talk(self):
        store = IdempotencyStore()
        counters = {}
        errors = []

        def worker(i):
            try:
                key = f"key-{i:08d}"
                c = counters.setdefault(key, {"n": 0})
                for _ in range(5):
                    store.execute(USER, ENDPOINT, key, fp(key.encode()),
                                  ok_handler_factory(c, body=key.encode()))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(64)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertTrue(all(c["n"] == 1 for c in counters.values()))
        self.assertEqual(len(store), 64)


class TestFingerprint(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(fp(), fp())

    def test_body_matters(self):
        self.assertNotEqual(fp(b"a"), fp(b"b"))

    def test_method_and_path_matter(self):
        self.assertNotEqual(fp(BODY, "POST", "/orders"), fp(BODY, "GET", "/orders"))
        self.assertNotEqual(fp(BODY, "POST", "/orders"), fp(BODY, "POST", "/order"))

    def test_str_and_bytes_body_equal(self):
        self.assertEqual(request_fingerprint("POST", "/p", "abc"),
                         request_fingerprint("POST", "/p", b"abc"))

    def test_empty_body_ok(self):
        request_fingerprint("GET", "/p", b"")


if __name__ == "__main__":
    unittest.main()

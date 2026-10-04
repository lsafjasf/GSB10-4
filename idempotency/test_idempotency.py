"""Self-tests for idempotency.py. Run: python3 test_idempotency.py -v"""
import json
import threading
import time
import unittest

from idempotency import (
    Decision,
    IdempotencyStore,
    fingerprint_json,
    handle_once,
    make_key,
)


class FakeClock:
    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def order_handler_factory(calls):
    """Simulated 'create order' handler; records every real execution."""
    def handler():
        calls.append(1)
        body = json.dumps(
            {"order_id": "ORD-%04d" % len(calls), "attempt": len(calls)},
            sort_keys=True,
        ).encode()
        return 201, body
    return handler


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.store = IdempotencyStore(
            ttl=3600.0, tombstone_ttl=7200.0, wait_timeout=2.0,
            clock=self.clock,
        )
        self.key = make_key("user-1", "POST /orders", "ck-abc")
        self.fp = fingerprint_json({"sku": "A1", "qty": 2})

    # 1. first request executes exactly once
    def test_first_request_executes(self):
        calls = []
        status, body = handle_once(self.store, self.key, self.fp,
                                   order_handler_factory(calls))
        self.assertEqual(status, 201)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(body)["attempt"], 1)

    # 2. duplicate replays byte-identical response (对拍)
    def test_duplicate_replays_identical_bytes(self):
        calls = []
        handler = order_handler_factory(calls)
        s1, b1 = handle_once(self.store, self.key, self.fp, handler)
        s2, b2 = handle_once(self.store, self.key, self.fp, handler)
        s3, b3 = handle_once(self.store, self.key, self.fp, handler)
        self.assertEqual(len(calls), 1, "handler must run exactly once")
        self.assertEqual((s1, b1), (s2, b2))
        self.assertEqual((s1, b1), (s3, b3))
        print("\n[对拍] first :", s1, b1)
        print("[对拍] replay1:", s2, b2, "identical:", (s1, b1) == (s2, b2))
        print("[对拍] replay2:", s3, b3, "identical:", (s1, b1) == (s3, b3))

    # 3. concurrent duplicate waits, then replays the same response
    def test_concurrent_duplicate_waits_and_replays(self):
        store = IdempotencyStore(ttl=60, wait_timeout=5.0)  # real clock
        calls = []

        def slow_handler():
            calls.append(1)
            time.sleep(0.3)
            return 200, b'{"order_id":"ORD-0001","attempt":1}'

        results = {}

        def first():
            results["first"] = handle_once(store, self.key, self.fp, slow_handler)

        def second():
            time.sleep(0.05)  # arrive while first is in flight
            results["second"] = handle_once(store, self.key, self.fp, slow_handler)

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(calls), 1, "handler must run exactly once")
        self.assertEqual(results["first"], results["second"])
        print("\n[对拍-并发] first :", results["first"])
        print("[对拍-并发] second:", results["second"],
              "identical:", results["first"] == results["second"])

    # 3b. concurrent duplicate that cannot wait gets 409, nothing executes twice
    def test_concurrent_duplicate_timeout_returns_409(self):
        store = IdempotencyStore(ttl=60, wait_timeout=0.1)
        calls = []

        def slow_handler():
            calls.append(1)
            time.sleep(0.5)
            return 200, b"ok"

        results = {}

        def first():
            results["first"] = handle_once(store, self.key, self.fp, slow_handler)

        def second():
            time.sleep(0.05)
            results["second"] = handle_once(store, self.key, self.fp, slow_handler)

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(results["second"][0], 409)
        self.assertEqual(results["first"][0], 200)
        self.assertEqual(len(calls), 1)

    # 4. same key + different payload => 422 conflict, handler not run
    def test_conflict_same_key_different_payload(self):
        calls = []
        handle_once(self.store, self.key, self.fp, order_handler_factory(calls))
        other_fp = fingerprint_json({"sku": "A1", "qty": 99})
        status, body = handle_once(self.store, self.key, other_fp,
                                   order_handler_factory(calls))
        self.assertEqual(status, 422)
        self.assertEqual(len(calls), 1)

    # 5. expired key replay => 410, never re-executed
    def test_expired_replay_is_not_treated_as_first(self):
        calls = []
        handler = order_handler_factory(calls)
        s1, b1 = handle_once(self.store, self.key, self.fp, handler)
        self.clock.advance(3600.0 + 1)  # past ttl, inside tombstone window
        s2, b2 = handle_once(self.store, self.key, self.fp, handler)
        self.assertEqual((s2, b2), (410, b'{"error":"idempotency_key_expired"}'))
        self.assertEqual(len(calls), 1, "expired replay must NOT re-execute")
        print("\n[对拍-过期] first :", s1, b1)
        print("[对拍-过期] replay:", s2, b2, "(handler calls:", len(calls), ")")

    # 5b. expiry beats conflict: expired key + different payload is still 410
    def test_expired_with_different_payload_is_expired_not_conflict(self):
        calls = []
        handle_once(self.store, self.key, self.fp, order_handler_factory(calls))
        self.clock.advance(3600.0 + 1)
        status, _ = handle_once(self.store, self.key,
                                fingerprint_json({"sku": "B9"}),
                                order_handler_factory(calls))
        self.assertEqual(status, 410)
        self.assertEqual(len(calls), 1)

    # 5c. after tombstone also dies, key is fully forgotten (documented edge)
    def test_tombstone_purge_allows_new_execution(self):
        calls = []
        handle_once(self.store, self.key, self.fp, order_handler_factory(calls))
        self.clock.advance(3600.0 + 7200.0 + 1)  # past ttl + tombstone_ttl
        status, _ = handle_once(self.store, self.key, self.fp,
                                order_handler_factory(calls))
        self.assertEqual(status, 201)
        self.assertEqual(len(calls), 2)

    # 6. scope isolation: same client key, different user/endpoint
    def test_scope_isolation(self):
        calls = []
        handler = order_handler_factory(calls)
        k_user2 = make_key("user-2", "POST /orders", "ck-abc")
        k_other_ep = make_key("user-1", "POST /refunds", "ck-abc")
        s1, _ = handle_once(self.store, self.key, self.fp, handler)
        s2, _ = handle_once(self.store, k_user2, self.fp, handler)
        s3, _ = handle_once(self.store, k_other_ep, self.fp, handler)
        self.assertEqual((s1, s2, s3), (201, 201, 201))
        self.assertEqual(len(calls), 3, "different scopes must not dedup")

    # 7. complete() with wrong fingerprint is rejected
    def test_complete_with_wrong_fingerprint_rejected(self):
        r = self.store.begin(self.key, self.fp)
        self.assertEqual(r.decision, Decision.EXECUTE)
        ok = self.store.complete(self.key, fingerprint_json({"x": 1}), 200, b"x")
        self.assertFalse(ok)

    # 8. abort() releases the slot so a retry may execute
    def test_abort_allows_retry(self):
        r = self.store.begin(self.key, self.fp)
        self.assertEqual(r.decision, Decision.EXECUTE)
        self.assertTrue(self.store.abort(self.key, self.fp))
        calls = []
        status, _ = handle_once(self.store, self.key, self.fp,
                                order_handler_factory(calls))
        self.assertEqual(status, 201)
        self.assertEqual(len(calls), 1)

    # 9. stored failure replays identically (retry sees same 500)
    def test_failure_response_replays_identically(self):
        calls = []

        def failing():
            calls.append(1)
            return 500, b'{"error":"db_down"}'

        s1, b1 = handle_once(self.store, self.key, self.fp, failing)
        s2, b2 = handle_once(self.store, self.key, self.fp, failing)
        self.assertEqual((s1, b1), (500, b'{"error":"db_down"}'))
        self.assertEqual((s1, b1), (s2, b2))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

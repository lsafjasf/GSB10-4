"""Golden ("对拍") data: deterministic scenarios and expected outcomes.

Each scenario records user scope, endpoint, key, request body, the
expected decision (execute / replay / 400 / 409-conflict / 410-expired /
202-in-progress), expected status code and expected body.  Bodies are
stored hex-encoded so byte equality is checked exactly.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from idempotency import (
    IdempotencyStore,
    IdempotencyError,
    InProgressPolicy,
    Response,
    request_fingerprint,
)
from server_helpers import decorate_replay, error_response

GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden.jsonl")

# Canonical server-side order response.
ORDER_BODY = b'{"order_id":"O-7788","status":"CREATED"}'
REFUND_BODY = b'{"refund_id":"R-1","status":"CREATED"}'


class FakeClock:
    def __init__(self):
        self.t = 1_700_000_000.0
    def __call__(self):
        return self.t
    def advance(self, dt):
        self.t += dt


def _row(case, user, endpoint, key, body, decision, status, body_out,
         replayed, note):
    return {
        "case": case,
        "user_id": user,
        "endpoint": endpoint,
        "key": key,
        "request_body_hex": body.hex(),
        "decision": decision,       # execute | replay | 400 | 409_conflict | 409_in_progress | 202 | 410
        "status": status,
        "response_body_hex": body_out.hex(),
        "replayed": replayed,
        "note": note,
    }


def build_rows():
    """Drive three stores through all scenarios and emit outcome rows."""
    clock = FakeClock()
    store = IdempotencyStore(ttl_seconds=60, clock=clock)
    rows = []

    def run(case, user, endpoint, key, body, handler_body=None,
            policy_store=None, note=""):
        s = policy_store if policy_store is not None else store
        hb = handler_body if handler_body is not None else ORDER_BODY
        try:
            resp, replayed = s.execute(
                user, endpoint, key,
                request_fingerprint("POST", endpoint.split(" ", 1)[1], body),
                lambda: Response(201, hb, (("Content-Type", "application/json"),)),
            )
            resp = decorate_replay(resp, replayed)
            decision = "replay" if replayed else "execute"
            rows.append(_row(case, user, endpoint, key, body, decision,
                             resp.status, resp.body, replayed, note))
        except IdempotencyError as exc:
            resp = error_response(exc)
            if exc.status_code == 400:
                decision = "400"
            elif exc.status_code == 410:
                decision = "410"
            elif exc.error_code == "IDEMPOTENCY_REQUEST_IN_PROGRESS":
                decision = "409_in_progress"
            else:
                decision = "409_conflict"
            rows.append(_row(case, user, endpoint, key, body, decision,
                             resp.status, resp.body, False, note))
        return rows[-1]

    # 1. first order
    r1 = run("01_first_order", "user-A", "POST /orders", "ord_9f2c7e1a-0001",
             b'{"sku":"A-1","qty":2}', note="handler runs, order created")
    # 2. exact duplicate -> byte-identical replay
    r2 = run("02_duplicate_order", "user-A", "POST /orders", "ord_9f2c7e1a-0001",
             b'{"sku":"A-1","qty":2}', note="replay must equal 01 byte-for-byte")
    assert r2["status"] == r1["status"]
    assert r2["response_body_hex"] == r1["response_body_hex"]
    assert r2["replayed"] is True
    # 3. same key, mutated body -> 409 conflict
    run("03_key_reuse_conflict", "user-A", "POST /orders", "ord_9f2c7e1a-0001",
        b'{"sku":"A-1","qty":3}', note="same key, different payload -> 409")
    # 4. another user using the same key string -> independent first request
    run("04_other_user_same_key", "user-B", "POST /orders", "ord_9f2c7e1a-0001",
        b'{"sku":"A-1","qty":2}', note="user scope isolation")
    # 5. same user, different endpoint, same key string -> independent
    run("05_other_endpoint_same_key", "user-A", "POST /refunds", "ord_9f2c7e1a-0001",
        b'{"order_id":"O-7788"}', handler_body=REFUND_BODY,
        note="endpoint scope isolation")
    # 6. malformed key -> 400
    run("06_bad_key_format", "user-A", "POST /orders", "bad key!",
        b'{}', note="invalid key rejected before any state change")
    # 7. duplicate of user-B -> identical to 04 (not 01!)
    r7 = run("07_other_user_replay", "user-B", "POST /orders", "ord_9f2c7e1a-0001",
             b'{"sku":"A-1","qty":2}', note="replay of user-B's own record")
    r4 = rows[3]
    assert r7["response_body_hex"] == r4["response_body_hex"]
    # 8. replay still valid just before TTL boundary
    clock.advance(60)
    run("08_replay_at_ttl_boundary", "user-A", "POST /orders", "ord_9f2c7e1a-0001",
        b'{"sku":"A-1","qty":2}', note="at exactly expires_at the record is stale -> 410")
    # 9. replay after expiry with SAME body -> 410, never a fresh execute
    run("09_expired_replay_same_body", "user-A", "POST /orders", "ord_9f2c7e1a-0001",
        b'{"sku":"A-1","qty":2}', note="expired key replay must not be treated as first")
    # 10. replay after expiry with DIFFERENT body -> still 410 (not conflict)
    run("10_expired_replay_diff_body", "user-A", "POST /orders", "ord_9f2c7e1a-0001",
        b'{"sku":"A-1","qty":9}', note="expiry takes precedence over conflict")
    # 11. concurrent duplicate while in flight (IN_PROGRESS policy -> 202)
    ip_store = IdempotencyStore(in_progress_policy=InProgressPolicy.IN_PROGRESS)
    started = threading.Event()
    release = threading.Event()

    def slow_order():
        started.set()
        release.wait(5)
        return Response(201, ORDER_BODY, (("Content-Type", "application/json"),))

    first_holder = {}

    def first_thread():
        resp, _ = ip_store.execute(
            "user-C", "POST /orders", "ord_concurren-tkey01",
            request_fingerprint("POST", "/orders", b'{"sku":"X"}'), slow_order)
        first_holder["resp"] = resp

    t = threading.Thread(target=first_thread)
    t.start()
    started.wait(5)
    run("11_concurrent_in_progress", "user-C", "POST /orders", "ord_concurren-tkey01",
        b'{"sku":"X"}', policy_store=ip_store,
        note="duplicate while first request is running -> 202")
    release.set()
    t.join()
    # 12. after completion, the same key replays the real 201 (not 202)
    r12 = run("12_replay_after_completion", "user-C", "POST /orders",
              "ord_concurren-tkey01", b'{"sku":"X"}', policy_store=ip_store,
              note="once finished, callers get the stored 201")
    assert r12["status"] == 201
    assert bytes.fromhex(r12["response_body_hex"]) == ORDER_BODY
    # 13. REJECT policy while in flight -> 409 IDEMPOTENCY_REQUEST_IN_PROGRESS
    rj_store = IdempotencyStore(in_progress_policy=InProgressPolicy.REJECT)
    started2 = threading.Event()
    release2 = threading.Event()

    def slow_order2():
        started2.set()
        release2.wait(5)
        return Response(201, ORDER_BODY, (("Content-Type", "application/json"),))

    t2 = threading.Thread(target=lambda: rj_store.execute(
        "user-D", "POST /orders", "ord_concurren-tkey02",
        request_fingerprint("POST", "/orders", b'{"sku":"Y"}'), slow_order2))
    t2.start()
    started2.wait(5)
    run("13_concurrent_rejected", "user-D", "POST /orders", "ord_concurren-tkey02",
        b'{"sku":"Y"}', policy_store=rj_store,
        note="duplicate while running, REJECT policy -> 409 in-progress")
    release2.set()
    t2.join()

    return rows


def write_golden():
    rows = build_rows()
    with open(GOLDEN_PATH, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    return rows


def read_golden():
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["generate", "verify"])
    args = p.parse_args()
    if args.cmd == "generate":
        rows = write_golden()
        print(f"wrote {len(rows)} golden rows -> {GOLDEN_PATH}")
    else:
        expected = read_golden()
        actual = build_rows()
        failures = 0
        if len(expected) != len(actual):
            print(f"FAIL: row count {len(expected)} != {len(actual)}")
            failures += 1
        for i, (e, a) in enumerate(zip(expected, actual)):
            if e != a:
                failures += 1
                print(f"FAIL row {i} ({e.get('case')}):")
                for k in sorted(set(e) | set(a)):
                    if e.get(k) != a.get(k):
                        print(f"  {k}: expected={e.get(k)!r} actual={a.get(k)!r}")
        # Determinism: generate a second time and diff.
        again = build_rows()
        if again != actual:
            failures += 1
            print("FAIL: second generation differs (non-deterministic)")
        if failures:
            raise SystemExit(f"{failures} mismatch(es)")
        # Explicit first==replay byte equality assertions (对拍核心).
        def find(case):
            return next(r for r in actual if r["case"] == case)
        pairs = [("01_first_order", "02_duplicate_order"),
                 ("04_other_user_same_key", "07_other_user_replay")]
        for a, b in pairs:
            ra, rb = find(a), find(b)
            assert ra["status"] == rb["status"]
            assert ra["response_body_hex"] == rb["response_body_hex"], (a, b)
        print(f"OK: {len(actual)} golden rows match, first==replay byte-identical")


if __name__ == "__main__":
    main()

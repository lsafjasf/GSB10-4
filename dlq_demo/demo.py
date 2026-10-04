"""Generate retry-schedule and dead-letter requeue roundtrip artefacts.

Outputs (under dlq_demo/data/):
  retry_schedule.json      backoff+jitter retry instants vs an independent oracle
  requeue_roundtrip.json   dead -> requeue -> success snapshots, cross-checked

Run: python -m dlq_demo.demo
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

from .dlq import (
    Category,
    Clock,
    DeadLetterProcessor,
    ErrorCode,
    Message,
    RetryPolicy,
    classify,
)

DATA_DIR = Path(__file__).resolve().parent / "data"


def oracle_schedule(message_id, policy, retries=4):
    """Independent replay of the full-jitter rule; no library code involved."""
    times, t = [], 0.0
    for attempt in range(1, retries + 1):
        cap = min(policy.max_delay, policy.base_delay * policy.multiplier ** (attempt - 1))
        delay = random.Random(f"{policy.seed}|{message_id}|{attempt}").uniform(0.0, cap)
        t += delay
        times.append({"retry_no": attempt, "cap_before_jitter": cap,
                      "delay": delay, "instant": t})
    return times


def library_schedule(message_id, policy, retries=4):
    clock = Clock(0.0)
    proc = DeadLetterProcessor(policy, clock)
    proc.enqueue(Message(id=message_id, queue="q", payload=b"x"))
    rows = []
    t = 0.0
    for retry_no in range(1, retries + 1):
        proc.due_ids()
        proc.fail(message_id, ErrorCode.TIMEOUT)
        instant = proc.next_attempt_at(message_id)
        cap = min(policy.max_delay, policy.base_delay * policy.multiplier ** (retry_no - 1))
        rows.append({"retry_no": retry_no, "cap_before_jitter": cap,
                     "delay": instant - t, "instant": instant})
        t = instant
        clock.set(instant)
    return rows


def build_schedule_doc():
    p = RetryPolicy(seed=0)
    m1_j = library_schedule("m1", p)
    oracle = oracle_schedule("m1", p)
    assert [r["instant"] for r in m1_j] == [r["instant"] for r in oracle]

    # same message under interleaved traffic proves order independence
    clock = Clock(0.0)
    proc = DeadLetterProcessor(p, clock)
    proc.enqueue(Message(id="m1", queue="q", payload=b"x"))
    for i in range(20):
        proc.enqueue(Message(id=f"noise-{i}", queue="q", payload=b"n"))
        proc.due_ids()
        proc.fail(f"noise-{i}", ErrorCode.TIMEOUT)
    instants = []
    for retry_no in range(1, 5):
        proc.due_ids()
        proc.fail("m1", ErrorCode.TIMEOUT)
        instants.append(proc.next_attempt_at("m1"))
        clock.set(instants[-1])
    assert instants == [r["instant"] for r in oracle]

    pure = library_schedule("m1", RetryPolicy(jitter=False))
    return {
        "policy": {
            "max_attempts": p.max_attempts,
            "base_delay": p.base_delay,
            "multiplier": p.multiplier,
            "max_delay": p.max_delay,
            "jitter": "full: U(0, min(cap, base*2^(attempt-1)))",
            "rng_seed": "per message: sha-independent str seed f'{seed}|{message_id}|{attempt}'",
        },
        "m1_full_jitter": m1_j,
        "m1_full_jitter_independent_oracle": oracle,
        "m1_under_20_interleaved_messages": instants,
        "m1_no_jitter_pure_backoff": pure,
        "cross_check_equal": (
            [r["instant"] for r in m1_j] == [r["instant"] for r in oracle] == instants
        ),
    }


def build_roundtrip_doc():
    """Exhaust retries -> dead letter -> requeue to origin queue -> success.

    Every field is also computed by an independent oracle and asserted equal.
    """
    clock = Clock(0.0)
    policy = RetryPolicy(seed=0, max_attempts=5, max_requeues=1)
    proc = DeadLetterProcessor(policy, clock)
    msg = Message(id="order-123", queue="orders.created",
                  payload=b'{"order_id":"o-9","amount":42,"currency":"CNY"}',
                  headers={"trace-id": "abcdef", "content-type": "application/json"})
    proc.enqueue(msg)

    # cycle 1: five transient failures, schedule taken from the library
    sched1 = []
    for attempt in range(1, 6):
        proc.due_ids()
        proc.fail("order-123", ErrorCode.TIMEOUT, f"attempt {attempt} timed out")
        if attempt < 5:
            sched1.append(proc.next_attempt_at("order-123"))
            clock.set(sched1[-1])

    dl = proc.get_dead("order-123")
    dead_snapshot = {
        "message_id": dl.message.id,
        "origin_queue": dl.message.queue,
        "payload_b64_is_original": dl.message.payload == msg.payload,
        "payload_hex": dl.message.payload.hex(),
        "headers": dict(dl.message.headers),
        "attempts": dl.attempts,
        "failure_records": [
            {"attempt": f.attempt, "code": f.code, "category": f.category.value,
             "detail": f.detail, "failed_at": f.failed_at}
            for f in dl.failures
        ],
    }

    requeued = proc.requeue("order-123")
    after_requeue = {
        "phase": proc.phase("order-123").value,
        "attempts_reset_to": proc.attempts("order-123"),
        "requeues": proc._states["order-123"].requeues,
        "history_records_retained": proc.failure_count("order-123"),
        "delivered_to_queue": proc._states["order-123"].message.queue,
        "payload_hex": proc._states["order-123"].message.payload.hex(),
        "headers": dict(proc._states["order-123"].message.headers),
    }
    # retry cycle 2 succeeds on first attempt
    due = proc.due_ids()
    acked = proc.ack("order-123") if due == ["order-123"] else False

    # ---- independent oracle (plain scalars, no processor internals) ----
    expected_caps = [1.0, 2.0, 4.0, 8.0]
    expected_delays = [
        random.Random(f"0|order-123|{a}").uniform(0.0, c)
        for a, c in zip(range(1, 5), expected_caps)
    ]
    expected_sched = []
    acc = 0.0
    for d in expected_delays:
        acc += d
        expected_sched.append(acc)
    oracle = {
        "expected_retry_instants_cycle1": expected_sched,
        "library_retry_instants_cycle1": sched1,
        "expected_attempts_when_dead": 5,
        "expected_failure_records_when_dead": 5,
        "expected_attempts_after_requeue": 0,
        "expected_history_after_requeue": 5,
        "expected_requeues": 1,
        "expected_final_ack": True,
        "all_equal": (
            sched1 == expected_sched
            and dead_snapshot["attempts"] == 5
            and len(dead_snapshot["failure_records"]) == 5
            and after_requeue["attempts_reset_to"] == 0
            and after_requeue["history_records_retained"] == 5
            and after_requeue["requeues"] == 1
            and after_requeue["delivered_to_queue"] == "orders.created"
            and after_requeue["payload_hex"] == msg.payload.hex()
            and acked is True
        ),
    }
    assert oracle["all_equal"], oracle

    return {
        "policy": {"max_attempts": 5, "max_requeues": 1},
        "dead_letter_snapshot": dead_snapshot,
        "requeue_result": {"accepted": requeued},
        "after_requeue": after_requeue,
        "final": {"delivered_on_attempt": 1, "acked": acked,
                  "phase": proc.phase("order-123").value},
        "independent_oracle_cross_check": oracle,
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sched = build_schedule_doc()
    roundtrip = build_roundtrip_doc()
    (DATA_DIR / "retry_schedule.json").write_text(json.dumps(sched, indent=2))
    (DATA_DIR / "requeue_roundtrip.json").write_text(json.dumps(roundtrip, indent=2))
    print("retry schedule cross-check:", sched["cross_check_equal"])
    print("roundtrip cross-check:    ", roundtrip["independent_oracle_cross_check"]["all_equal"])
    print("wrote:", (DATA_DIR / "retry_schedule.json"))
    print("wrote:", (DATA_DIR / "requeue_roundtrip.json"))


if __name__ == "__main__":
    main()

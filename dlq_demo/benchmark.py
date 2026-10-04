"""Benchmark: 100k failed messages -> classification time + dead-letter memory.

Run: python -m dlq_demo.benchmark
"""

from __future__ import annotations

import json
import time
import tracemalloc
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
N = 100_000
PAYLOAD_BYTES = 256

# 70% retryable transient, 25% non-retryable poison, 5% unknown codes
RETRYABLE_CODES = [ErrorCode.TIMEOUT, ErrorCode.UNAVAILABLE, ErrorCode.RATE_LIMITED,
                   ErrorCode.CONNECTION_RESET, ErrorCode.INTERNAL]
NON_RETRYABLE_CODES = [ErrorCode.VALIDATION, ErrorCode.DESERIALIZATION,
                       ErrorCode.AUTH, ErrorCode.NOT_FOUND]


def code_for(i: int) -> str:
    r = i % 20
    if r < 14:
        return RETRYABLE_CODES[r % len(RETRYABLE_CODES)]
    if r < 19:
        return NON_RETRYABLE_CODES[r % len(NON_RETRYABLE_CODES)]
    return f"unknown-code-{i % 97}"


def main():
    policy = RetryPolicy(seed=0, jitter=False)  # deterministic schedule
    codes = [code_for(i) for i in range(N)]

    # 1) pure classification throughput
    t0 = time.perf_counter()
    counts = {Category.RETRYABLE: 0, Category.NON_RETRYABLE: 0}
    for c in codes:
        counts[classify(c)] += 1
    t1 = time.perf_counter()
    classify_s = t1 - t0

    # 2) end-to-end: enqueue + all attempts + failure records, DLQ memory
    clock = Clock(0.0)
    proc = DeadLetterProcessor(policy, clock)
    tracemalloc.start()
    base = tracemalloc.take_snapshot()
    t2 = time.perf_counter()

    payload = bytes(97 + (i % 26) for i in range(PAYLOAD_BYTES))
    for i in range(N):
        proc.enqueue(Message(id=f"msg-{i}", queue="bench.q", payload=payload,
                             headers={"k": "v"}))
    # process wave by wave: deliver everything due, fail it, jump the clock
    while proc._states:
        due = proc.due_ids()
        if not due:
            nxt = min(proc._states[m].next_attempt_at for m in proc._states)
            clock.set(nxt)
            continue
        for mid in due:
            i = int(mid.split("-")[1])
            proc.fail(mid, codes[i], "bench failure")
        # advance past the longest wave delay (pure backoff, jitter off)
        clock.advance(policy.max_delay)

    t3 = time.perf_counter()
    end_to_end_s = t3 - t2

    snap = tracemalloc.take_snapshot()
    tracemalloc.stop()
    dlq_bytes = sum(stat.size for stat in snap.compare_to(base, "filename"))
    dlq_blocks = sum(stat.count for stat in snap.compare_to(base, "filename"))

    dead = proc.dead_count()
    assert dead == N, (dead, N)
    per_msg = dlq_bytes / N

    report = {
        "messages": N,
        "payload_bytes_per_message": PAYLOAD_BYTES,
        "policy": {"max_attempts": policy.max_attempts, "jitter": policy.jitter},
        "classification": {
            "seconds": round(classify_s, 4),
            "per_message_us": round(classify_s / N * 1e6, 3),
            "messages_per_second": round(N / classify_s),
            "retryable": counts[Category.RETRYABLE],
            "non_retryable": counts[Category.NON_RETRYABLE],
        },
        "end_to_end": {
            "seconds": round(end_to_end_s, 4),
            "per_message_us": round(end_to_end_s / N * 1e6, 3),
            "attempts_total": sum(s.attempts for s in proc._dead.values()),
            "failure_records_total": sum(len(s.failures) for s in proc._dead.values()),
        },
        "dead_letter_table_memory": {
            "bytes": dlq_bytes,
            "mib": round(dlq_bytes / 2**20, 2),
            "per_message_bytes": round(per_msg, 1),
            "blocks": dlq_blocks,
            "note": "tracemalloc delta; includes messages, states, failure records, dicts, heap",
        },
        "dead_count": dead,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "benchmark.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

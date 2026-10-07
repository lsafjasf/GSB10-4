"""Generate the delivery artifacts:

1. Reproducible retry-time series (seeded jitter).
2. Replay differential data （对拍): requeued message vs original/DLQ.
3. Classification timing and DLQ memory for 100,000 failed messages.

Stdlib only. Run: python3 bench.py
"""

import random
import sys
import time
import tracemalloc

from dlq import FailureOutcome, FailureReason, Processor, RetryPolicy, Status

POLICY = RetryPolicy()
T0 = 1_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def section(title):
    print(f"\n=== {title} ===")


def retry_series():
    section("Retry time series (seed=42, t0=1000000.0)")
    proc = Processor(Clock(), random.Random(42), POLICY)
    proc.submit("m", "orders", b'{"order":123}')
    print(f"{'attempt':>7} {'code':>13} {'base_s':>8} {'delay_s':>9} {'scheduled_at':>15}")
    for attempt in range(1, POLICY.max_attempts):
        proc.begin_delivery("m")
        proc.on_failure("m", FailureReason("HTTP_500", "", proc._clock.t))
        rec = proc.record("m")
        delay = rec.next_delivery_at - proc._clock.t
        print(f"{attempt:>7} {'HTTP_500':>13} {POLICY.base_for(attempt):>8.3f} "
              f"{delay:>9.4f} {rec.next_delivery_at:>15.4f}")
        proc._clock.t = rec.next_delivery_at
    proc.begin_delivery("m")
    out = proc.on_failure("m", FailureReason("HTTP_500", "", proc._clock.t))
    print(f"{POLICY.max_attempts:>7} {'HTTP_500':>13} "
          f"{'--':>8} {'--':>9} -> {out.value}")
    # reproducibility check
    def seq(seed):
        p = Processor(Clock(), random.Random(seed), POLICY)
        p.submit("m", "q", b"")
        out = []
        for _ in range(POLICY.max_attempts - 1):
            p.begin_delivery("m")
            p.on_failure("m", FailureReason("HTTP_500", "", p._clock.t))
            out.append(round(p.record("m").next_delivery_at, 6))
            p._clock.t = p.record("m").next_delivery_at
        return out
    assert seq(42) == seq(42), "same seed must reproduce identical schedule"
    print("reproducible: seed=42 produces identical sequence twice")


def replay_oracle():
    section("Replay differential data （对拍： requeued vs original)")
    proc = Processor(Clock(), random.Random(42), POLICY)
    payload, headers = b'{"order":123,"amount":4500}', {"trace-id": "a-1", "x": "y"}
    proc.submit("m", "orders", payload, headers)
    proc.begin_delivery("m")
    proc.on_failure("m", FailureReason("PERMISSION_DENIED", "billing not linked",
                                       proc._clock.t))
    e = proc.dead_letter("m")
    rec = proc.replay("m")
    proc.begin_delivery("m")
    rows = [
        ("message_id", rec.message_id, e.message_id, rec.message_id == e.message_id),
        ("queue (original)", rec.queue, e.queue, rec.queue == e.queue),
        ("payload", rec.payload, e.payload, rec.payload == e.payload),
        ("headers", rec.headers, e.headers, rec.headers == e.headers),
        ("enqueued_at", rec.enqueued_at, e.enqueued_at,
         rec.enqueued_at == e.enqueued_at),
        ("dead_at", "-", e.dead_at, e.dead_at == T0),
        ("failures_before_replay", "-", len(e.failures), len(e.failures) == 1),
        ("failures[0].code", "-", e.failures[0].reason.code,
         e.failures[0].reason.code == "PERMISSION_DENIED"),
        ("replay_count (live)", rec.replay_count, "-", rec.replay_count == 1),
        ("replayed (dlq entry)", "-", e.replayed, e.replayed == 1),
        ("attempts after redelivery", rec.attempts, "-", rec.attempts == 1),
    ]
    print(f"{'field':>26} {'requeued':>30} {'dlq/original':>30} {'match':>6}")
    for field, a, b, ok in rows:
        sa, sb = str(a)[:28], str(b)[:28]
        print(f"{field:>26} {sa:>30} {sb:>30} {str(bool(ok)):>6}")
    proc.on_success("m")
    print("post-replay outcome:", rec.status.value, "| dlq history retained:",
          len(e.failures) == 1)


def deep_size(obj, seen=None):
    size = sys.getsizeof(obj)
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return 0
    seen.add(oid)
    if isinstance(obj, dict):
        size += sum(deep_size(k, seen) + deep_size(v, seen) for k, v in obj.items())
    elif isinstance(obj, (list, tuple, set, frozenset)):
        size += sum(deep_size(v, seen) for v in obj)
    elif hasattr(obj, "__dict__"):
        size += deep_size(vars(obj), seen)
    elif hasattr(obj, "__slots__"):
        size += sum(deep_size(getattr(obj, s), seen) for s in obj.__slots__
                    if hasattr(obj, s))
    return size


def benchmark(n=100_000):
    section(f"Benchmark: {n:,} failed messages")
    proc = Processor(Clock(), random.Random(42), POLICY)
    codes = ["HTTP_500", "NETWORK_TIMEOUT", "HTTP_429", "THROTTLED",
             "VALIDATION_ERROR", "PERMISSION_DENIED", "HTTP_404",
             "DESERIALIZATION_ERROR", "HTTP_503", "MALFORMED_PAYLOAD"]

    start = time.perf_counter()
    for i in range(n):
        proc.submit(f"msg-{i}", "orders", b'{"k":"payload-data"}',
                    {"trace-id": f"t-{i}"})
    t_submit = time.perf_counter() - start

    # Each message: first delivery + failure classification. Retryable ones
    # are driven through all remaining attempts; non-retryable dead-letter
    # immediately.
    start = time.perf_counter()
    classifications = [0, 0]  # retryable, non-retryable
    for i in range(n):
        mid = f"msg-{i}"
        code = codes[i % len(codes)]
        proc.begin_delivery(mid)
        out = proc.on_failure(mid, FailureReason(code, "bench", T0))
        classifications[0 if out is FailureOutcome.RETRY_SCHEDULED else 1] += 1
    t_classify = time.perf_counter() - start
    # process retries up to exhaustion (advance clock to make all due)
    proc._clock.t += 10_000
    start = time.perf_counter()
    for _ in range(POLICY.max_attempts - 1):
        due = proc.due(proc._clock.t)
        for mid in due:
            proc.begin_delivery(mid)
            code = "HTTP_500" if proc.record(mid).failures[0].reason.code in (
                "HTTP_500", "NETWORK_TIMEOUT", "HTTP_429", "THROTTLED", "HTTP_503"
            ) else "VALIDATION_ERROR"
            proc.on_failure(mid, FailureReason(code, "bench", proc._clock.t))
    t_retries = time.perf_counter() - start

    dlq_size = deep_size(proc.dead_letters)
    full_size = deep_size(proc)
    tracemalloc.start()
    proc2 = Processor(Clock(), random.Random(1), POLICY)
    for i in range(n):
        proc2.submit(f"msg-{i}", "orders", b'{"k":"payload-data"}',
                     {"trace-id": f"t-{i}"})
        proc2.begin_delivery(f"msg-{i}")
        proc2.on_failure(f"msg-{i}", FailureReason(codes[i % len(codes)], "", T0))
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    n_retry, n_dead_first = classifications
    print(f"submit {n:>8,}: {t_submit*1000:8.1f} ms "
          f"({t_submit/n*1e6:6.2f} us/op)")
    print(f"classify first failure: {t_classify*1000:8.1f} ms "
          f"({t_classify/n*1e6:6.2f} us/op)  "
          f"retryable={n_retry:,} non_retryable={n_dead_first:,}")
    print(f"drive retries to terminal state: {t_retries*1000:8.1f} ms")
    print(f"DLQ entries: {len(proc.dead_letters):,}")
    print(f"DLQ table memory (recursive sizeof): "
          f"{dlq_size/1e6:8.1f} MB ({dlq_size/len(proc.dead_letters):7.0f} B/entry)")
    print(f"whole processor memory (recursive sizeof): {full_size/1e6:8.1f} MB")
    print(f"tracemalloc peak for immediate-DLQ workload: {peak/1e6:8.1f} MB "
          f"({peak/n:7.0f} B/msg)")
    # classify-only throughput, pure path
    from dlq import classify
    reasons = [FailureReason(codes[i % len(codes)]) for i in range(n)]
    start = time.perf_counter()
    for r in reasons:
        classify(r)
    t = time.perf_counter() - start
    print(f"pure classify() x{n:,}: {t*1000:8.1f} ms ({t/n*1e6:6.3f} us/op)")


if __name__ == "__main__":
    retry_series()
    replay_oracle()
    benchmark()

"""Benchmark: 100,000 transactions.

Measures pure *scheduling* cost (classification + jitter draw + budget check);
backoff is advanced on a virtual clock, so no wall time is spent sleeping.
Workload mix and jitter are seeded -> results are reproducible.
"""

import random
import statistics
import time as realtime
from collections import Counter

from txn_retry import (
    ConflictKind,
    Retryer,
    RetryPolicy,
    Status,
    TxnConflict,
)

N = 100_000
# Workload mix by terminal pattern:
#   n=0..3 conflicts then success, persistent (exhaust attempts),
#   or a non-retryable failure on the first attempt.
WEIGHTS = {
    0: 0.900,
    1: 0.060,
    2: 0.025,
    3: 0.010,
    "persistent": 0.004,
    "non_retryable": 0.001,
}

POLICY = RetryPolicy(base_delay=0.010, max_delay=1.0, max_attempts=8,
                     budget=5.0)


class VirtualClock:
    __slots__ = ("now",)

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make_workload(rng):
    """Return a list of N scripts describing each transaction."""
    r = random.Random(20261005)
    table = list(WEIGHTS.items())
    scripts = []
    for i in range(N):
        x = r.random()
        acc = 0.0
        chosen = table[-1][0]
        for key, w in table:
            acc += w
            if x < acc:
                chosen = key
                break
        scripts.append(chosen)
    return scripts


def main():
    scripts = make_workload(None)

    retry_counter = Counter()
    status_counter = Counter()
    backoffs = [0.0] * N
    rng = random.Random(99)

    t0 = realtime.perf_counter()
    for i, pattern in enumerate(scripts):
        clock = VirtualClock()
        txn_rng = random.Random(rng.getrandbits(64))
        retryer = Retryer(
            policy=POLICY, clock=clock.time, sleep=clock.sleep, rng=txn_rng
        )

        if pattern == "non_retryable":
            def body(_: object = None):
                raise TxnConflict(ConflictKind.CONSTRAINT_VIOLATION, "dup key")
        elif pattern == "persistent":
            def body(_: object = None):
                raise TxnConflict(ConflictKind.DEADLOCK)
        else:
            remaining = {"n": pattern}

            def body(_: object = None):
                if remaining["n"] > 0:
                    remaining["n"] -= 1
                    raise TxnConflict(ConflictKind.DEADLOCK)
                return True

        res = retryer.run(body)
        retry_counter[res.num_retries] += 1
        status_counter[res.status.value] += 1
        backoffs[i] = res.elapsed
    wall = realtime.perf_counter() - t0

    total_attempts = 0
    for retries, count in retry_counter.items():
        total_attempts += (retries + 1) * count

    print(f"transactions:        {N:>12,}")
    print(f"wall scheduling time:{wall:>11.4f} s")
    print(f"  per transaction:   {wall / N * 1e6:>11.3f} us")
    total_att = total_attempts
    print(f"total attempts:      {total_att:>12,}")
    print(f"  per attempt:       {wall / total_att * 1e6:>11.3f} us")
    print()
    print("retry-count distribution:")
    for retries in sorted(retry_counter):
        count = retry_counter[retries]
        label = str(retries) if retries < POLICY.max_attempts - 1 \
            else f"{retries} (attempt cap)"
        print(f"  retries={label:<18} {count:>8,}  {count / N:10.4%}")
    print()
    print("terminal status:")
    for status, count in sorted(status_counter.items()):
        print(f"  {status:<20} {count:>8,}  {count / N:10.4%}")
    print()
    print(f"virtual backoff consumed: {sum(backoffs):>10.3f} s "
          f"(sum across txns)")
    print(f"  per txn: mean={statistics.mean(backoffs) * 1000:.4f} ms "
          f"p50={statistics.median(backoffs) * 1000:.4f} ms "
          f"max={max(backoffs):.4f} s")
    print(f"  budget per txn: {POLICY.budget:.3f} s; "
          f"any overrun: {max(backoffs) > POLICY.budget}")


if __name__ == "__main__":
    main()

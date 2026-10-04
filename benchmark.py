"""Benchmark: 100k submissions against a pool with queue capacity 100.

Reports wall time, throughput, rejection totals and the rejection-rate
distribution across 10 batches of 10k submissions, per policy.
"""

import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from threadpool import Policy, RejectedExecutionError, ThreadPool  # noqa: E402

TOTAL = 100_000
BATCH = 10_000
CAPACITY = 100
WORKERS = 4


def work():
    # small CPU task so the queue actually fills under load
    total = 0
    for i in range(200):
        total += i * i
    return total


def run(policy):
    # rejection events bucketed into the 10 submission window during which
    # they occur (captures DISCARD_OLDEST evictions, not only refused submits)
    buckets = [0] * (TOTAL // BATCH)
    state = {"i": 0}

    def on_rejected(_rec):
        buckets[state["i"] // BATCH] += 1

    pool = ThreadPool(workers=WORKERS, capacity=CAPACITY, policy=policy,
                      on_rejected=on_rejected)
    t0 = time.perf_counter()
    for i in range(TOTAL):
        state["i"] = i
        try:
            pool.submit(work)
        except RejectedExecutionError:
            pass
    submit_time = time.perf_counter() - t0
    pool.shutdown()
    total_time = time.perf_counter() - t0

    a = pool.accounting()
    assert a["conserved"], a
    assert a["submitted"] == TOTAL
    assert a["submitted"] == a["completed"] + a["rejected"]
    assert sum(buckets) == a["rejected"], (buckets, a)
    batch_rejected = buckets

    rates = [n / BATCH * 100 for n in batch_rejected]
    return {
        "policy": policy.value,
        "submit_s": submit_time,
        "total_s": total_time,
        "submits_per_s": TOTAL / submit_time,
        "completed": a["completed"],
        "rejected": a["rejected"],
        "reject_pct": a["rejected"] / TOTAL * 100,
        "reject_by_reason": a["reject_by_reason"],
        "batch_rates": rates,
        "rate_min": min(rates),
        "rate_max": max(rates),
        "rate_mean": statistics.mean(rates),
        "rate_stdev": statistics.pstdev(rates),
    }


def main():
    print(f"config: workers={WORKERS} capacity={CAPACITY} submissions={TOTAL}\n")
    results = [run(p) for p in (Policy.ABORT, Policy.DISCARD,
                                Policy.CALLER_RUNS, Policy.DISCARD_OLDEST)]
    hdr = f"{'policy':<15}{'submit(s)':>10}{'total(s)':>10}{'submit/s':>11}{'completed':>11}{'rejected':>10}{'rej%':>8}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(f"{r['policy']:<15}{r['submit_s']:>10.3f}{r['total_s']:>10.3f}"
              f"{r['submits_per_s']:>11.0f}{r['completed']:>11}{r['rejected']:>10}"
              f"{r['reject_pct']:>7.2f}%")

    print("\nrejection-rate distribution across 10 batches of 10k (%):")
    hdr = f"{'policy':<15}{'min':>8}{'mean':>8}{'max':>8}{'stdev':>8}  per-batch"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        batches = " ".join(f"{x:.1f}" for x in r["batch_rates"])
        print(f"{r['policy']:<15}{r['rate_min']:>8.2f}{r['rate_mean']:>8.2f}"
              f"{r['rate_max']:>8.2f}{r['rate_stdev']:>8.2f}  {batches}")

    print("\nrejection reasons:")
    for r in results:
        print(f"  {r['policy']:<15}{r['reject_by_reason']}")

    print("\nconservation check (submitted == completed + rejected + queued + running):")
    for r in results:
        print(f"  {r['policy']:<15}100000 == {r['completed']} + {r['rejected']} + 0 + 0  OK")


if __name__ == "__main__":
    main()

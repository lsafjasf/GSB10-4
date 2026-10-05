"""Self-check + benchmark entry point.

Runs:
1. A migration (32 -> 64 partitions) with interleaved writes and dumps the
   before/after ordering cross-check (order_check.txt).
2. 1,000,000 messages over 1,000 keys: routing time and per-partition load
   distribution, including skew/hot-key scenarios (load_distribution.txt).
"""

import os
import random
import statistics
import time

from .broker import Broker, Consumer, MigrationInProgress, Producer
from .router import partition_for

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def distribution_summary(loads):
    n = len(loads)
    total = sum(loads)
    mean = total / n
    stdev = statistics.pstdev(loads)
    return {
        "partitions": n,
        "total": total,
        "min": min(loads),
        "max": max(loads),
        "mean": mean,
        "stdev": stdev,
        "max_over_mean": max(loads) / mean,
    }


def run_order_check():
    """32 -> 64 doubling, writes interleaved before/during/after migration."""
    rng = random.Random(42)
    broker = Broker(32, mode="dual_read")
    producer = Producer(broker)
    consumer = Consumer(broker)
    keys = ["k%04d" % i for i in range(1000)] + [None]  # include empty key

    produced = {k: [] for k in keys}
    produced[None] = []

    def send_batch(count, tag):
        for _ in range(count):
            key = rng.choice(keys)
            seq = producer.send(key, tag)
            produced[key].append(seq)

    send_batch(20000, "before")
    consumer.poll(20000)

    broker.begin_migration(64)
    send_batch(20000, "during")
    # Deliberately interleave dual reads while writes keep landing on the
    # new topology; the reorder buffer must keep per-key order intact.
    consumer.poll(10000)
    send_batch(20000, "during")
    consumer.poll(20000)
    send_batch(20000, "during")

    assert broker.old_drained(consumer) or True  # may still need final drain
    drained = consumer.drain()
    assert broker.old_drained(consumer)
    broker.end_migration(consumer)

    send_batch(20000, "after")
    consumer.drain()

    ok = True
    sample = []
    for key in keys[:5] + [None]:
        expected = produced[key]
        got = consumer.delivered_sequence(key)
        match = expected == got
        ok &= match
        sample.append((key, expected, got, match))
    # Full cross-check for every key
    for key in keys:
        if produced[key] != consumer.delivered_sequence(key):
            ok = False
            break

    lines = []
    lines.append("# Ordering cross-check: 32 -> 64 partitions (dual_read)")
    lines.append(
        "# total messages=%d, keys=%d, all per-key sequences match: %s"
        % (sum(len(v) for v in produced.values()), len(produced), ok)
    )
    lines.append("#")
    lines.append("# columns: key | produced seqs (before->during->after)")
    lines.append("#          | delivered seqs after dual-read reorder | match")
    for key, expected, got, match in sample:
        lines.append("key=%r" % key)
        lines.append("  produced : %s" % expected)
        lines.append("  delivered: %s" % got)
        lines.append("  match    : %s" % match)
    lines.append("ALL_KEYS_IN_ORDER=%s" % ok)
    return ok, "\n".join(lines)


def run_reject_order_check():
    """reject mode: writes during migration must raise, order preserved."""
    broker = Broker(4, mode="reject")
    producer = Producer(broker)
    consumer = Consumer(broker)
    produced = []
    for i in range(50):
        produced.append(producer.send("k1", "before"))
    broker.begin_migration(8)
    rejected = False
    try:
        producer.send("k1", "during")
    except MigrationInProgress:
        rejected = True
    consumer.drain()
    broker.end_migration(consumer)
    for i in range(50):
        produced.append(producer.send("k1", "after"))
    consumer.drain()
    ok = (
        rejected
        and consumer.delivered_sequence("k1") == produced
        and producer._seq["k1"] == 100
    )
    return ok, rejected


def run_routing_benchmark(num_messages=1_000_000, num_keys=1000,
                          num_partitions=32):
    keys = ["k%04d" % i for i in range(num_keys)]

    scenarios = []

    # 1. uniform key distribution
    rng = random.Random(7)
    loads = [0] * num_partitions
    t0 = time.perf_counter()
    for _ in range(num_messages):
        key = keys[rng.randrange(num_keys)]
        loads[partition_for(key, num_partitions)] += 1
    elapsed = time.perf_counter() - t0
    scenarios.append(("uniform_1000keys", loads, elapsed))

    # 2. zipf-like skew: key i chosen with probability 1/(i+1)
    weights = [1.0 / (i + 1) for i in range(num_keys)]
    total_w = sum(weights)
    cum, acc = [], 0.0
    for w in weights:
        acc += w / total_w
        cum.append(acc)
    loads = [0] * num_partitions
    t0 = time.perf_counter()
    for _ in range(num_messages):
        r = rng.random()
        lo, hi = 0, num_keys - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if cum[mid] < r:
                lo = mid + 1
            else:
                hi = mid
        loads[partition_for(keys[lo], num_partitions)] += 1
    elapsed = time.perf_counter() - t0
    scenarios.append(("zipf_skew_1000keys", loads, elapsed))

    # 3. single hot key: 50% of traffic, other 50% spread over the rest
    hot = "HOT-KEY"
    hot_partition = partition_for(hot, num_partitions)
    loads = [0] * num_partitions
    t0 = time.perf_counter()
    for i in range(num_messages):
        if i % 2 == 0:
            key = hot
        else:
            key = keys[i % num_keys]
        loads[partition_for(key, num_partitions)] += 1
    elapsed = time.perf_counter() - t0
    scenarios.append(("single_hot_key_50pct", loads, elapsed))

    # 4. similar-prefix keys (avalanche check)
    prefix_keys = ["user:%07d" % i for i in range(num_keys)]
    loads = [0] * num_partitions
    t0 = time.perf_counter()
    for i in range(num_messages):
        key = prefix_keys[i % num_keys]
        loads[partition_for(key, num_partitions)] += 1
    elapsed = time.perf_counter() - t0
    scenarios.append(("similar_prefix_keys", loads, elapsed))

    return scenarios, hot_partition


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    ok, order_text = run_order_check()
    reject_ok, rejected = run_reject_order_check()
    order_text += (
        "\nreject_mode_writes_blocked=%s reject_mode_order_ok=%s"
        % (rejected, reject_ok)
    )
    with open(os.path.join(DATA_DIR, "order_check.txt"), "w") as f:
        f.write(order_text + "\n")

    scenarios, hot_partition = run_routing_benchmark()
    lines = ["# Routing benchmark: 1,000,000 messages / 1,000 keys / N=32"]
    lines.append("# mapping: partition = crc32(key) % 32")
    for name, loads, elapsed in scenarios:
        s = distribution_summary(loads)
        lines.append("")
        lines.append("## scenario: %s" % name)
        lines.append("routing_seconds=%.4f  ns_per_route=%.1f"
                     % (elapsed, elapsed * 1e9 / s["total"]))
        lines.append(
            "partitions=%d total=%d min=%d max=%d mean=%.1f "
            "stdev=%.2f max/mean=%.3f"
            % (s["partitions"], s["total"], s["min"], s["max"],
               s["mean"], s["stdev"], s["max_over_mean"])
        )
        lines.append("loads=%s" % loads)
    lines.append("")
    lines.append("hot_key_partition=%d (a single hot key cannot be split "
                 "across partitions without breaking its ordering)"
                 % hot_partition)
    text = "\n".join(lines)
    with open(os.path.join(DATA_DIR, "load_distribution.txt"), "w") as f:
        f.write(text + "\n")

    print(text)
    print()
    print(order_text)
    if not ok or not reject_ok:
        raise SystemExit("ordering check FAILED")


if __name__ == "__main__":
    main()

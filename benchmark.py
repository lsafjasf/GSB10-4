"""Benchmark 1M sends/acks and estimate bounded unacked-window memory."""

import argparse
import json
import os
import platform
import random
import resource
import sys
from time import perf_counter

from ack_window import AckWindow

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def run_case(total, capacity, shuffle, seed):
    rng = random.Random(seed)
    w = AckWindow(capacity)
    sent = 0
    send_seconds = 0.0
    ack_seconds = 0.0

    while sent < total:
        block = min(capacity, total - sent)
        t0 = perf_counter()
        for _ in range(block):
            w.send()
        t1 = perf_counter()
        send_seconds += t1 - t0

        first = sent + 1
        sent += block
        acks = list(range(first, sent + 1))
        if shuffle:
            rng.shuffle(acks)

        t0 = perf_counter()
        for seq in acks:
            w.ack(seq)
        t1 = perf_counter()
        ack_seconds += t1 - t0

    stats = w.stats()
    total_seconds = send_seconds + ack_seconds
    return {
        "seconds": total_seconds,
        "send_seconds": send_seconds,
        "ack_seconds": ack_seconds,
        "operations_per_second": total * 2 / total_seconds,
        "send_ops_per_second": total / send_seconds,
        "ack_ops_per_second": total / ack_seconds,
        "nanoseconds_per_operation": total_seconds * 1_000_000_000 / (total * 2),
        "accepted_acks": stats["acks_accepted"],
        "duplicate_acks": stats["acks_duplicate"],
        "advance_events": stats["advance_events"],
        "max_window_used": stats["max_window_used"],
        "max_unacked": stats["max_unacked"],
    }


def probe_memory(capacity, seed):
    rng = random.Random(seed)
    w = AckWindow(capacity)
    for _ in range(capacity):
        w.send()

    checkpoints = {1, max(1, capacity // 4), capacity // 2, 3 * capacity // 4, capacity}
    samples = [{"after_acks": 0, "bytes": w.memory_bytes()}]
    acks = list(range(1, capacity + 1))
    rng.shuffle(acks)

    for processed, seq in enumerate(acks, 1):
        w.ack(seq)
        if processed in checkpoints:
            samples.append({"after_acks": processed, "bytes": w.memory_bytes()})

    peak = max(sample["bytes"] for sample in samples)
    return {
        "window_capacity": capacity,
        "structural_peak_bytes": peak,
        "structural_peak_mib": peak / 1024 / 1024,
        "samples": samples,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=1_000_000)
    parser.add_argument("--window", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=20261005)
    args = parser.parse_args()

    result = {
        "config": {
            "messages": args.messages,
            "window_capacity": args.window,
            "seed": args.seed,
            "payload": None,
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "implementation": platform.python_implementation(),
        },
        "cases": {
            "in_order": run_case(args.messages, args.window, False, args.seed),
            "out_of_order": run_case(args.messages, args.window, True, args.seed),
        },
        "memory": probe_memory(args.window, args.seed),
    }
    result["memory"]["process_ru_maxrss_kib"] = resource.getrusage(
        resource.RUSAGE_SELF
    ).ru_maxrss

    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "benchmark.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=True)

    print(f"messages: {args.messages:,}, window: {args.window:,}")
    for name, case in result["cases"].items():
        print(
            f"{name}: {case['seconds']:.3f}s, "
            f"{case['operations_per_second']:,.0f} send+ack ops/s"
        )
    print(
        "window structural peak: "
        f"{result['memory']['structural_peak_mib']:.2f} MiB"
    )
    print(f"report: {path}")


if __name__ == "__main__":
    main()

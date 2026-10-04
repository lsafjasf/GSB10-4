"""Generate count-trace and out-of-order/in-order equivalence data."""

import csv
import os
import random

from ack_window import AckWindow

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def count_trace():
    w = AckWindow(5)
    for _ in range(5):
        w.send()

    rows = []
    for step, seq in enumerate((2, 2, 1, 2, 3, 3, 5, 4, 4), 1):
        result = w.ack(seq)
        snap = w.snapshot()
        rows.append(
            {
                "step": step,
                "ack_seq": seq,
                "status": result.status,
                "released": result.released,
                "base": snap.base,
                "unacked": snap.unacked,
                "window_used": snap.window_used,
                "acked_ahead": snap.acked_ahead,
            }
        )
    return rows


def run_boundaries(total, capacity, shuffle, seed):
    rng = random.Random(seed)
    w = AckWindow(capacity)
    sent = 0
    rows = []

    while sent < total:
        block = min(capacity, total - sent)
        block_seqs = [w.send() for _ in range(block)]
        sent += block

        acks = list(block_seqs)
        if shuffle:
            rng.shuffle(acks)
        for seq in acks:
            w.ack(seq)

        snap = w.snapshot()
        rows.append(
            {
                "block": len(rows) + 1,
                "sent": sent,
                "base": snap.base,
                "unacked": snap.unacked,
                "window_used": snap.window_used,
                "acked_ahead": snap.acked_ahead,
                "accepted": w.stats()["acks_accepted"],
            }
        )
    return w, rows


def write_count_trace(rows):
    path = os.path.join(DATA_DIR, "count_trace.csv")
    fields = [
        "step",
        "ack_seq",
        "status",
        "released",
        "base",
        "unacked",
        "window_used",
        "acked_ahead",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_equivalence(total, capacity, seed):
    shuffled_w, shuffled_rows = run_boundaries(total, capacity, True, seed)
    _, in_order_rows = run_boundaries(total, capacity, False, seed)

    if shuffled_rows != in_order_rows:
        raise AssertionError("out-of-order and in-order boundary states differ")

    rows = []
    for shuffled, in_order in zip(shuffled_rows, in_order_rows):
        row = {"block": shuffled["block"], "sent": shuffled["sent"]}
        for prefix, source in (("shuffled", shuffled), ("in_order", in_order)):
            for field in ("base", "unacked", "window_used", "acked_ahead", "accepted"):
                row[f"{prefix}_{field}"] = source[field]
        row["match"] = 1
        rows.append(row)

    path = os.path.join(DATA_DIR, "equivalence.csv")
    fields = ["block", "sent"]
    for prefix in ("shuffled", "in_order"):
        fields.extend(
            [
                f"{prefix}_base",
                f"{prefix}_unacked",
                f"{prefix}_window_used",
                f"{prefix}_acked_ahead",
                f"{prefix}_accepted",
            ]
        )
    fields.append("match")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path, rows, shuffled_w.stats()


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    trace_path = write_count_trace(count_trace())
    equivalence_path, rows, stats = write_equivalence(1000, 32, 20261005)
    print(f"count trace: {trace_path} ({9} acks)")
    print(f"equivalence: {equivalence_path} ({len(rows)} matched block boundaries)")
    print(f"final accepted acks: {stats['acks_accepted']}")


if __name__ == "__main__":
    main()

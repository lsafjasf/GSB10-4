"""100k-item benchmark + per-item status table export (stdlib only).

Run:  python3 benchmark.py            # 100000 items
      python3 benchmark.py 100000 out/status_table_100k.csv
"""

import csv
import os
import random
import sys
import time
from collections import Counter

from batch_retry import (
    Item,
    ItemError,
    ItemStatus,
    NON_RETRYABLE_CODES,
    RETRYABLE_CODES,
    submit_batch_with_retry,
)
from simulated_service import SimulatedBatchService


def build(n, seed=20261005):
    rng = random.Random(seed)
    items = [Item(f"it-{i:06d}", payload=i) for i in range(n)]
    plan = {}
    for it in items:
        r = rng.random()
        if r < 0.88:
            script = []                                   # success on first try
        elif r < 0.96:
            script = [ItemError("RATE_LIMIT"), "OK"]      # 1 retry -> success
        elif r < 0.98:
            script = [ItemError("SERVER_5XX"), ItemError("TIMEOUT"), "OK"]
        elif r < 0.99:
            script = [ItemError("UNAVAILABLE")] * 20      # always retryable
        else:
            script = [ItemError(rng.choice(sorted(NON_RETRYABLE_CODES)))]
        plan[it.item_id] = script
    return items, plan


def main(n=100_000, csv_path=None, max_retries=4):
    items, plan = build(n)
    # Inject one whole-batch timeout on submit call #2 (the first retry round)
    # to prove ambiguous transport failures end fully decided.
    svc = SimulatedBatchService(plan, transport_fail_on_calls={2})

    t0 = time.perf_counter()
    out = submit_batch_with_retry(svc, items, max_retries=max_retries)
    elapsed = time.perf_counter() - t0

    status_counts = Counter(r.status for r in out.table.values())
    retry_dist = Counter(out.retried_items.get(iid, 0) for iid in out.table)
    decided_by = Counter(r.decided_by for r in out.table.values())
    total_item_calls = sum(r.attempts for r in out.table.values())

    print(f"items={n}  max_retries={max_retries}  submit_rounds={out.rounds}")
    print(f"wall_time={elapsed:.3f}s  ({elapsed * 1000 / n:.4f} ms/item)")
    print(f"item-submissions total={total_item_calls} "
          f"resubmit_rate={total_item_calls / n - 1:.4%}")
    print("final status:")
    for st in ItemStatus:
        print(f"  {st.value:22s} {status_counts.get(st, 0)}")
    print("retries-per-item distribution (k = number of retry rounds):")
    cum = 0
    for k in range(0, max_retries + 1):
        c = retry_dist.get(k, 0)
        cum += c
        print(f"  k={k}  count={c}  pct={c / n:.4%}  cumulative={cum / n:.4%}")
    print(f"decided_by: {dict(decided_by)}")
    print(f"side_effects={len(svc.applied_ledger)} "
          f"unique={len(set(svc.applied_ledger))} "
          f"duplicates={len(svc.applied_ledger) - len(set(svc.applied_ledger))}")

    if csv_path:
        os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["item_id", "status", "attempts", "retries",
                        "last_error", "decided_by"])
            for iid in sorted(out.table):
                r = out.table[iid]
                w.writerow([iid, r.status.value, r.attempts,
                            out.retried_items.get(iid, 0),
                            r.last_error.code if r.last_error else "",
                            r.decided_by])
        print(f"status table written: {csv_path}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    path = sys.argv[2] if len(sys.argv) > 2 else None
    main(n, path)

"""Differential check （对拍）: batch+retry vs item-by-item submission.

Prints a concrete comparison for a fixed seed and exits non-zero on mismatch.

Run:  python3 diff_check.py [seed] [n]
"""

import random
import sys

from batch_retry import (
    Item,
    ItemError,
    ItemStatus,
    NON_RETRYABLE_CODES,
    RETRYABLE_CODES,
    submit_batch_with_retry,
    submit_each_independently,
)
from simulated_service import SimulatedBatchService


def random_plan(seed, n):
    rng = random.Random(seed)
    plan = {}
    for i in range(n):
        script = []
        while rng.random() < 0.35:
            script.append(ItemError(rng.choice(sorted(RETRYABLE_CODES))))
        if rng.random() < 0.15:
            script.append(ItemError(rng.choice(sorted(NON_RETRYABLE_CODES))))
        plan[f"it-{i:04d}"] = script
    return plan


def main(seed=42, n=1000, max_retries=8):
    items = [Item(f"it-{i:04d}", payload=i) for i in range(n)]

    svc_b = SimulatedBatchService(random_plan(seed, n))
    out_b = submit_batch_with_retry(svc_b, items, max_retries=max_retries)

    svc_e = SimulatedBatchService(random_plan(seed, n))
    out_e = submit_each_independently(svc_e, items, max_retries=max_retries)

    mismatches = []
    for it in items:
        rb, re_ = out_b.table[it.item_id], out_e.table[it.item_id]
        if (rb.status, rb.attempts) != (re_.status, re_.attempts):
            mismatches.append((it.item_id, rb.status, re_.status))

    ledger_equal = sorted(svc_b.applied_ledger) == sorted(svc_e.applied_ledger)
    no_dupes = len(svc_b.applied_ledger) == len(set(svc_b.applied_ledger))

    print(f"seed={seed} n={n} max_retries={max_retries}")
    print(f"batch path : rounds={out_b.rounds} "
          f"side_effects={len(svc_b.applied_ledger)}")
    print(f"single path: rounds={out_e.rounds} "
          f"side_effects={len(svc_e.applied_ledger)}")
    print(f"status+attempts mismatches: {len(mismatches)}")
    print(f"side-effect ledgers equal (as sets w/ multiplicity): {ledger_equal}")
    print(f"no duplicated side effects: {no_dupes}")

    print("\nsample per-item rows (batch | single):")
    print(f"{'item_id':10s} {'batch_status':22s} {'att':>3s}   "
          f"{'single_status':22s} {'att':>3s}")
    rng = random.Random(seed + 1)
    for it in rng.sample(items, 8):
        rb, re_ = out_b.table[it.item_id], out_e.table[it.item_id]
        print(f"{it.item_id:10s} {rb.status.value:22s} {rb.attempts:>3d}   "
              f"{re_.status.value:22s} {re_.attempts:>3d}")

    ok = not mismatches and ledger_equal and no_dupes
    print(f"\nDIFFERENTIAL RESULT: {'IDENTICAL' if ok else 'MISMATCH'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 42
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
    main(seed, n)

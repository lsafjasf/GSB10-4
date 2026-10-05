"""Self-tests for IndexedPriorityQueue.

Run:  python3 test_indexed_heap.py
Sections:
  1. Step-by-step invariant trace (sift-up / sift-down / equal-key paths)
  2. Edge cases: increase-only, decrease-only, equal keys, same-value update,
     extreme keys
  3. Differential test against a naive rebuild-the-heap implementation
  4. 1,000,000-operation benchmark (wall time + peak RSS)
"""

import math
import os
import random
import resource
import time

from indexed_heap import Empty, IndexedPriorityQueue


PASS = 0
FAIL = 0


def check(cond, label):
    global PASS, FAIL
    assert cond, label
    PASS += 1


# ----------------------------------------------------------------- section 1
def trace_step(q, label):
    ok = q.check_invariant()
    keys = [(e[2], e[0]) for e in q._heap]
    print(f"  invariant OK={ok}  size={len(q):2d}  after {label:34s} heap={keys}")


def section_invariant_trace():
    print("== 1. Step-by-step invariant trace ==")
    q = IndexedPriorityQueue()
    trace_step(q, "initial empty heap")
    for item, key in [("a", 50), ("b", 30), ("c", 40), ("d", 10), ("e", 60)]:
        q.push(item, key)
        trace_step(q, f"push({item!r},{key}) [sift up]")
    # decrease key: c 40 -> 5  must sift up
    q.update_key("c", 5)
    trace_step(q, "update c: 40 -> 5  [sift up]")
    # increase key: d 10 -> 100 must sift down
    q.update_key("d", 100)
    trace_step(q, "update d: 10 -> 100 [sift down]")
    # same value: no movement
    q.update_key("b", 30)
    trace_step(q, "update b: 30 -> 30  [no-op]")
    # increase key of the root: a 50 -> 90 sift down
    q.update_key("a", 90)
    trace_step(q, "update a: 50 -> 90  [sift down root]")
    drained = []
    while q:
        drained.append(q.pop())
        trace_step(q, f"pop -> {drained[-1]!r}")
    check(drained == [("c", 5), ("b", 30), ("e", 60), ("a", 90), ("d", 100)],
          "trace pop order")
    q.check_invariant()
    print(f"   final pop order: {drained}\n")


# ----------------------------------------------------------------- section 2
def section_edge_cases():
    print("== 2. Edge cases ==")

    # 2a. increase-only updates
    q = IndexedPriorityQueue()
    for i in range(100):
        q.push(i, i)
    for i in range(100):
        q.update_key(i, i + 1000)        # every update sifts down
        q.check_invariant()
    order = [q.pop()[0] for _ in range(100)]
    check(order == list(range(100)), "increase-only keeps order 0..99")
    print("   increase-only:              OK (100 items, each updated upward)")

    # 2b. decrease-only updates
    q = IndexedPriorityQueue()
    for i in range(100):
        q.push(i, i + 1000)
    for i in range(100):
        q.update_key(i, i)              # every update sifts up
        q.check_invariant()
    order = [q.pop()[0] for _ in range(100)]
    check(order == list(range(100)), "decrease-only keeps order 0..99")
    print("   decrease-only:              OK (100 items, each updated downward)")

    # 2c. equal keys must not lose or duplicate elements
    q = IndexedPriorityQueue()
    for i in range(200):
        q.push(i, 7)
    q.check_invariant()
    for i in range(200):
        q.update_key(i, 7)              # update to the same value
    q.check_invariant()
    check(len(q) == 200, "equal keys: size preserved")
    drained = [q.pop()[0] for _ in range(200)]
    check(sorted(drained) == list(range(200)), "equal keys: no loss/duplication")
    check(len(drained) == len(set(drained)), "equal keys: items unique")
    print("   equal keys / same-value:    OK (200 items, 200 same-value updates)")

    # 2d. extreme keys
    q = IndexedPriorityQueue()
    extremes = [
        ("neg_inf", float("-inf")),
        ("pos_inf", float("inf")),
        ("zero", 0),
        ("huge_float", 1.7e308),
        ("tiny_float", -1.7e308),
        ("huge_int", 10 ** 400),
        ("neg_huge_int", -(10 ** 400)),
    ]
    for item, key in extremes:
        q.push(item, key)
        q.check_invariant()
    q.update_key("zero", float("-inf"))   # ties with neg_inf: sift up, seq breaks tie
    q.check_invariant()
    q.update_key("neg_inf", float("inf"))  # ties with pos_inf: sift down
    q.check_invariant()
    q.update_key("huge_float", math.inf)
    q.check_invariant()
    q.update_key("pos_inf", -1.7e308)
    q.check_invariant()
    order = []
    while q:
        order.append(q.pop()[0])
    check(len(order) == 7, "extreme keys: no loss")
    check(order[0] in ("zero", "tiny_float", "pos_inf") and
          order[-1] in ("neg_inf", "pos_inf", "huge_float"),
          "extreme keys: -inf/inf placed correctly")
    print(f"   extreme keys:               OK pop order {order}")

    # 2e. errors
    q = IndexedPriorityQueue()
    try:
        q.pop(); raise AssertionError("expected Empty")
    except Empty:
        pass
    q.push("x", 1)
    try:
        q.push("x", 2); raise AssertionError("expected ValueError")
    except ValueError:
        pass
    print("   empty pop / duplicate push: OK\n")


# ----------------------------------------------------------------- section 3
class NaivePQ:
    """Reference implementation: stores a dict and rebuilds ordering from
    scratch for every pop (heapify on (key, insertion_seq))."""

    def __init__(self):
        self.keys = {}
        self.order = {}
        self.seq = 0

    def push(self, item, key):
        assert item not in self.keys
        self.keys[item] = key
        self.order[item] = self.seq
        self.seq += 1

    def update_key(self, item, new_key):
        self.keys[item] = new_key        # insertion seq unchanged, like indexed heap

    def pop(self):
        item = min(self.keys, key=lambda x: (self.keys[x], self.order[x]))
        key = self.keys.pop(item)
        del self.order[item]
        return item, key

    def peek(self):
        item = min(self.keys, key=lambda x: (self.keys[x], self.order[x]))
        return item, self.keys[item]

    def __len__(self):
        return len(self.keys)


def section_differential():
    print("== 3. Differential test vs naive rebuild ==")
    rng = random.Random(20261005)
    rounds = 400
    max_ops = 250
    total_ops = 0
    mismatches = 0
    sample = None
    for r in range(rounds):
        q = IndexedPriorityQueue()
        ref = NaivePQ()
        live = []
        for _ in range(max_ops):
            r_pick = rng.random()
            if len(live) < 5 or (r_pick < 0.35 and len(live) < 60):
                item = f"v{rng.randrange(10 ** 6)}"
                if item in q:
                    continue
                key = rng.randint(-50, 50)
                q.push(item, key)
                ref.push(item, key)
                live.append(item)
            elif r_pick < 0.80 and live:
                item = rng.choice(live)
                old = q.get_key(item)
                mode = rng.random()
                if mode < 0.35:          # decrease
                    new_key = old - rng.randint(1, 30)
                elif mode < 0.70:        # increase
                    new_key = old + rng.randint(1, 30)
                elif mode < 0.85:        # same value
                    new_key = old
                else:                    # extreme
                    new_key = rng.choice([float("-inf"), float("inf"), 0,
                                          10 ** 6, -(10 ** 6)])
                q.update_key(item, new_key)
                ref.update_key(item, new_key)
            elif live:
                got = q.pop()
                want = ref.pop()
                live.remove(got[0])
                total_ops += 1
                if got != want:
                    mismatches += 1
                    if sample is None:
                        sample = (r, got, want)
                    break
            q.check_invariant()
            if live:
                check(q.peek() == ref.peek(), f"peek agrees, round {r}")
        else:
            # drain fully and compare whole sequence
            while q:
                got = q.pop()
                want = ref.pop()
                total_ops += 1
                if got != want:
                    mismatches += 1
                    if sample is None:
                        sample = (r, got, want)
                    break
            check(len(q) == len(ref) == 0, f"both empty after drain, round {r}")
            continue
        break

    check(mismatches == 0,
          f"differential mismatches: {mismatches}, first={sample}")
    print(f"   rounds={rounds}, max ops/round={max_ops}, "
          f"compared pops={total_ops}, mismatches={mismatches}")
    print(f"   (includes decrease / increase / same-value / extreme updates)\n")


# ----------------------------------------------------------------- section 4
def section_benchmark(n=1_000_000):
    print(f"== 4. Benchmark: {n:,} operations ==")
    rng = random.Random(42)
    q = IndexedPriorityQueue()
    n_push = 500_000
    n_other = n - n_push

    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.perf_counter()

    for i in range(n_push):
        q.push(i, rng.randint(0, 10 ** 9))

    pushed = n_push
    updated_up = updated_down = updated_same = updated_extreme = popped = 0
    next_id = n_push
    ops_done = 0
    while ops_done < n_other:
        r = rng.random()
        if r < 0.45 and q:             # update an existing element
            while True:
                item = rng.randrange(next_id)
                if item in q:
                    break
            old = q.get_key(item)
            mode = rng.random()
            if mode < 0.40:
                q.update_key(item, old - rng.randint(1, 10 ** 6)); updated_down += 1
            elif mode < 0.80:
                q.update_key(item, old + rng.randint(1, 10 ** 6)); updated_up += 1
            elif mode < 0.95:
                q.update_key(item, old); updated_same += 1
            else:
                q.update_key(item, rng.choice([float("-inf"), float("inf")]))
                updated_extreme += 1
        elif r < 0.75 and q:           # pop
            q.pop(); popped += 1
        else:                          # push to keep heap populated
            q.push(next_id, rng.randint(0, 10 ** 9))
            next_id += 1; pushed += 1
        ops_done += 1

    elapsed = time.perf_counter() - t0
    rss_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    q.check_invariant()
    final_size = len(q)
    # sanity: drain to make sure nothing is lost
    while q:
        q.pop()
    total_updates = updated_down + updated_up + updated_same + updated_extreme
    print(f"   pushes={pushed:,}  updates-down={updated_down:,}  "
          f"updates-up={updated_up:,}  updates-same={updated_same:,}  "
          f"updates-extreme={updated_extreme:,}  pops={popped:,}")
    print(f"   total operations = {pushed + total_updates + popped:,} "
          f"(updates={total_updates:,})")
    print(f"   heap size before drain={final_size:,}, drained cleanly, final invariant OK")
    print(f"   wall time: {elapsed:.3f} s ({n / elapsed:,.0f} ops/s)")
    print(f"   peak RSS: {rss_peak / 1024:.1f} MiB "
          f"(baseline at start {rss0 / 1024:.1f} MiB)")


if __name__ == "__main__":
    section_invariant_trace()
    section_edge_cases()
    section_differential()
    section_benchmark()
    print(f"ALL CHECKS PASSED ({PASS} assertions)")

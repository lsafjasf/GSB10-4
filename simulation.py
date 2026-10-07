"""Two-level flood simulation: completion-time distribution of low-priority
tasks under a sustained high-priority flood.

Model: single non-preemptive server, service times U[0.5, 1.5] (mean 1).
High-priority tasks arrive as a Poisson process with rate LAMBDA = 0.98
(98% utilization: heavy, stable load). 500 low-priority tasks are injected
every 25 time units. All randomness is seeded -> fully reproducible.

Checks the fairness invariant for every low task L enqueued at t0:
no task enqueued at or after t0 + (P_HIGH - P_LOW) * AGING may be dequeued
before L.
"""

import heapq
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from priority_queue import PriorityQueue, Task

AGING = 100.0
P_HIGH, P_LOW = 10, 0
LAMBDA = 0.98          # high-priority arrival rate (service rate = 1)
N_LOW = 500
LOW_PERIOD = 25.0
LOW_START = 50.0
SVC_LO, SVC_HI = 0.5, 1.5
WINDOW = (P_HIGH - P_LOW) * AGING   # bounded overtaking window = 1000

rng = random.Random(20261007)


def main() -> None:
    q = PriorityQueue(aging_interval=AGING, clock=lambda: now[0])
    now = [0.0]
    events: list[tuple[float, int, str]] = []
    seq = 0

    def push(t, kind):
        nonlocal seq
        heapq.heappush(events, (t, seq, kind))
        seq += 1

    push(rng.expovariate(LAMBDA), "high_arrival")
    push(LOW_START, "low_arrival")

    next_id = 0
    busy_until = None
    serving = None
    lows_pending: list[Task] = []     # enqueued, not yet dequeued
    low_waits: list[float] = []
    low_completions: list[float] = []
    low_arrivals = 0
    violations = 0
    max_backlog = 0

    def try_start_service():
        nonlocal busy_until, serving
        if busy_until is None:
            task = q.dequeue(now=now[0])
            if task is not None:
                serving = task
                svc = rng.uniform(SVC_LO, SVC_HI)
                busy_until = now[0] + svc
                push(busy_until, "service_done")

    while low_arrivals < N_LOW or lows_pending or serving is not None:
        t, _, kind = heapq.heappop(events)
        now[0] = t
        if kind == "high_arrival":
            q.enqueue(Task(task_id=next_id, priority=P_HIGH), now=t)
            next_id += 1
            push(t + rng.expovariate(LAMBDA), "high_arrival")
            try_start_service()
        elif kind == "low_arrival":
            task = q.enqueue(Task(task_id=next_id, priority=P_LOW), now=t)
            lows_pending.append(task)
            next_id += 1
            low_arrivals += 1
            if low_arrivals < N_LOW:
                push(t + LOW_PERIOD, "low_arrival")
            try_start_service()
        else:  # service_done
            done = serving
            serving = None
            busy_until = None
            if done.priority == P_LOW:
                lows_pending.remove(done)
                low_waits.append(t - done.enqueued_at - 0)  # incl. service
                low_completions.append(t)
            else:
                # Fairness invariant vs every still-waiting low task.
                for low in lows_pending:
                    if done.enqueued_at >= low.enqueued_at + WINDOW:
                        violations += 1
            max_backlog = max(max_backlog, len(q))
            try_start_service()

    waits = sorted(low_waits)
    n = len(waits)

    def pct(p):
        return waits[min(n - 1, int(p * n))]

    print("=== Two-level flood simulation ===")
    print(f"aging_interval={AGING}  P_HIGH={P_HIGH}  P_LOW={P_LOW}  "
          f"lambda={LAMBDA}  service~U[{SVC_LO},{SVC_HI}]")
    print(f"bounded overtaking window W = (P_HIGH-P_LOW)*AGING = {WINDOW:.0f}")
    print(f"low tasks completed: {n}")
    print(f"fairness invariant violations: {violations}")
    print(f"max queue backlog observed: {max_backlog}")
    print("low-priority completion latency (completion - enqueue):")
    print(f"  min   {waits[0]:10.1f}")
    print(f"  p50   {pct(0.50):10.1f}")
    print(f"  p90   {pct(0.90):10.1f}")
    print(f"  p95   {pct(0.95):10.1f}")
    print(f"  p99   {pct(0.99):10.1f}")
    print(f"  max   {waits[-1]:10.1f}")
    print(f"  mean  {statistics.fmean(waits):10.1f}")
    print(f"  stdev {statistics.pstdev(waits):10.1f}")
    if violations:
        sys.exit("FAIRNESS INVARIANT VIOLATED")
    print("OK: no task arriving after t0+W ever overtook a low task.")


if __name__ == "__main__":
    main()

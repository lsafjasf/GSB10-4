"""Demonstration: prints the submit() outcome for every required scenario,
including each shutdown phase, plus the counter-conservation accounting."""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from threadpool import Policy, RejectReason, State, ThreadPool


def show(label, r):
    if r.accepted:
        print(f"  {label:<28} -> accepted  future={'ok' if r.future else '-'}  "
              f"executed_by_caller={r.executed_by_caller}")
    else:
        print(f"  {label:<28} -> REJECTED  reason={r.reason.value:<20} "
              f"policy={r.policy.value}")


def block_worker(pool):
    release = threading.Event()
    pool.submit(lambda: release.wait(5))
    while pool.stats().running != 1:
        time.sleep(0.001)
    return release


print("== 1. queue not full (capacity=2, worker idle) ==")
pool = ThreadPool(workers=1, capacity=2, policy=Policy.ABORT)
show("submit #1", pool.submit(lambda: 1))
show("submit #2", pool.submit(lambda: 2))
pool.shutdown()
print(f"  stats: {pool.stats()}")

print("\n== 2. queue full, one submit under each policy ==")
for policy in (Policy.ABORT, Policy.CALLER_RUNS, Policy.DISCARD_OLDEST, Policy.DISCARD):
    pool = ThreadPool(workers=1, capacity=1, policy=policy)
    release = block_worker(pool)
    pool.submit(lambda: "in-queue")            # fills the queue
    r = pool.submit(lambda: "the new task")    # queue full -> policy applies
    show(f"{policy.value}", r)
    if policy is Policy.DISCARD_OLDEST:
        print("    (evicted task's Future raises RejectedExecution with "
              "reason=discarded_by_policy)")
    release.set()
    pool.shutdown()
    s = pool.stats()
    print(f"    accounting: submitted={s.submitted} completed={s.completed} "
          f"rejected={s.rejected} running={s.running} queued={s.queued} "
          f"conserved={s.conserved}")

print("\n== 3. submissions during shutdown phases ==")
pool = ThreadPool(workers=1, capacity=4, policy=Policy.ABORT)
release = block_worker(pool)
pool.submit(lambda: "queued during draining")
pool.shutdown(wait=False)
print(f"  phase: {State.SHUTTING_DOWN.name} (shutdown() called, draining)")
show("submit while shutting down", pool.submit(lambda: None))
release.set()
pool.shutdown(wait=True)
print(f"  phase: {State.TERMINATED.name} (all workers exited)")
show("submit after terminate", pool.submit(lambda: None))

pool = ThreadPool(workers=1, capacity=4, policy=Policy.ABORT)
release = block_worker(pool)
pool.shutdown_now(wait=False)
print(f"  phase: {State.STOP.name} (shutdown_now(), workers exiting)")
show("submit while stopped", pool.submit(lambda: None))
release.set()
pool.shutdown(wait=True)

print("\n== 4. conservation accounting at a random mid-run snapshot ==")
pool = ThreadPool(workers=3, capacity=10, policy=Policy.ABORT)
for _ in range(1000):
    pool.submit(lambda: time.sleep(0.0005))
time.sleep(0.01)
s = pool.stats()
print(f"  mid-run snapshot: {s}")
print(f"  check: {s.submitted} == {s.completed} + {s.rejected} + "
      f"{s.running} + {s.queued} = "
      f"{s.completed + s.rejected + s.running + s.queued} "
      f"-> {'OK' if s.conserved else 'BROKEN'}")
pool.shutdown()
s = pool.stats()
print(f"  after drain:     submitted={s.submitted} completed={s.completed} "
      f"rejected={s.rejected} running={s.running} queued={s.queued} "
      f"-> {'OK' if s.conserved else 'BROKEN'}")

"""Benchmark: 100k multi-stage executions.

Measures wall time and memory bounds of the state store + journals.

Run:  python3 bench.py
"""

import gc
import sys
import time
import tracemalloc

from saga import Executor, Stage, Store

N_TASKS = 100_000
N_STAGES = 4
WRITES_PER_STAGE = 2


def make_stages(task_no):
    stages = []
    for s in range(N_STAGES):
        def action(ctx, t=task_no, s=s):
            for w in range(WRITES_PER_STAGE):
                ctx.write(f"k{(t * N_STAGES + s + w) % 4096}", f"v{t}-{s}-{w}")
        stages.append(Stage(f"s{s}", action))
    return stages


def run(label, drop_journals):
    store = Store()
    ex = Executor(store)
    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()
    peak_live_tasks = 0
    for t in range(N_TASKS):
        task_id = f"task-{t}"
        result = ex.run(make_stages(t), task_id=task_id)
        assert result.state == "COMMITTED", result
        if drop_journals:
            ex.forget(task_id)
        else:
            peak_live_tasks = max(peak_live_tasks, ex.live_task_count())
    elapsed = time.perf_counter() - t0
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"[{label}]")
    print(f"  tasks                 : {N_TASKS:,} x {N_STAGES} stages x {WRITES_PER_STAGE} writes")
    print(f"  wall time             : {elapsed:.3f} s  ({N_TASKS / elapsed:,.0f} tasks/s, "
          f"{elapsed / N_TASKS * 1e6:.2f} us/task)")
    print(f"  tracemalloc peak      : {peak / 1024 / 1024:.2f} MiB")
    print(f"  tracemalloc final     : {current / 1024 / 1024:.2f} MiB")
    print(f"  store keys            : {len(store._versions):,}")
    print(f"  store versions        : {store.total_version_count():,}")
    print(f"  live journals at end  : {ex.live_task_count():,}")
    if not drop_journals:
        print(f"  peak live journals    : {peak_live_tasks:,}")
        per_task = peak / N_TASKS
        print(f"  journal+state per task: ~{per_task:.0f} B "
              f"(upper bound = O(live_tasks x stages))")
    print()
    return elapsed, peak


if __name__ == "__main__":
    print(f"python {sys.version.split()[0]}\n")
    run("journals dropped after terminal state (production pattern)", True)
    run("journals retained (worst-case upper bound)", False)

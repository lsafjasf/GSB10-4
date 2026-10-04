"""Restart cross-check ("对拍"): continuous run vs restart-after-every-op.

Runs the SAME deterministic scenario twice:

  * continuous: one DelayedQueue instance for the whole scenario.
  * restart:    the queue is closed and re-opened after every operation,
                simulating a process crash/restart between any two ops.

Both runs must produce byte-identical delivery traces. Traces and the
scenario are written to docs/data/ so the result is inspectable.

Run:  python3 scripts/restart_crosscheck.py
"""

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from delayq import DelayedQueue, CANCELLED, ALREADY_DELIVERED, NOT_FOUND

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "data")


def make_scenario(seed: int = 20261005, n_ops: int = 4000):
    rng = random.Random(seed)
    ops = []
    live = []          # ids currently scheduled (may already be delivered)
    next_id = 0
    now = 0
    for _ in range(n_ops):
        roll = rng.random()
        if roll < 0.55:
            # schedule: mostly near-term, some far future, some past-due
            due = now + rng.choice([0, 0, 1, 2, 5, 10, 100, 10_000])
            if rng.random() < 0.05:
                due = now - rng.randint(1, 5)  # already expired
            ops.append(("schedule", f"m{next_id}", due,
                        f"payload-{next_id}"))
            live.append(f"m{next_id}")
            next_id += 1
        elif roll < 0.75 and live:
            ops.append(("cancel", rng.choice(live)))
        elif roll < 0.80:
            ops.append(("cancel", f"ghost-{rng.randint(0, 50)}"))
        elif roll < 0.97:
            now += rng.choice([0, 1, 2, 5, 20])
            ops.append(("pump", now))
        else:
            ops.append(("snapshot",))
    ops.append(("pump", now + 20_000))  # drain everything
    return ops


def run(ops, workdir, restart_every_op: bool):
    if os.path.exists(workdir):
        import shutil
        shutil.rmtree(workdir)
    q = DelayedQueue(workdir)
    trace = []

    def reopen():
        nonlocal q
        q.close()
        q = DelayedQueue(workdir)

    for op in ops:
        if op[0] == "schedule":
            seq = q.schedule(op[1], op[2], op[3])
            trace.append(f"SCHED {op[1]} due={op[2]} -> seq={seq}")
        elif op[0] == "cancel":
            r = q.cancel(op[1])
            trace.append(f"CANCEL {op[1]} -> {r.status} seq={r.seq}")
        elif op[0] == "pump":
            got = []
            q.pump(op[1], lambda rec: got.append(
                f"{rec.id}@due={rec.due}#seq={rec.seq}"))
            trace.append(f"PUMP {op[1]} -> [{', '.join(got)}]")
        elif op[0] == "snapshot":
            q.snapshot()
            trace.append("SNAPSHOT")
        if restart_every_op:
            reopen()
    q.close()
    return "\n".join(trace) + "\n"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    ops = make_scenario()
    scenario_txt = "\n".join(repr(op) for op in ops) + "\n"

    base = os.path.join(OUT_DIR, "crosscheck-work")
    trace_a = run(ops, base + "-continuous", restart_every_op=False)
    trace_b = run(ops, base + "-restart", restart_every_op=True)

    for name, content in [
        ("crosscheck_scenario.txt", scenario_txt),
        ("crosscheck_continuous.trace", trace_a),
        ("crosscheck_restart.trace", trace_b),
    ]:
        with open(os.path.join(OUT_DIR, name), "w") as fh:
            fh.write(content)

    import shutil
    shutil.rmtree(base + "-continuous", ignore_errors=True)
    shutil.rmtree(base + "-restart", ignore_errors=True)

    if trace_a == trace_b:
        print(f"OK: traces identical ({len(trace_a.splitlines())} trace "
              f"lines, {len(ops)} ops)")
        return 0
    a, b = trace_a.splitlines(), trace_b.splitlines()
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            print(f"MISMATCH at trace line {i}:\n  cont:   {x}\n  restart:{y}")
            break
    return 1


if __name__ == "__main__":
    sys.exit(main())

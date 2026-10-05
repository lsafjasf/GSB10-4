"""Real-time hard-exit demo (wall clock, daemon worker threads).

Scenarios covered in one process:
  * 2 stuck tasks        -> only force-exit can resolve them
  * 2 long cancelable    -> checkpoint / rollback during the drain phase
  * 3 tasks still queued -> disposition with 0 steps applied

The process terminates with os._exit(42), which is the real guarantee that
nothing (stuck threads, unfinished cleanup) can keep the release alive past
the shutdown budget. A final JSON line "REPORT {...}" is emitted first.

Run:  python3 demo_real_exit.py
"""
from __future__ import annotations

import json
import os
import sys
import time

from graceful_shutdown import (
    Policy,
    ShutdownConfig,
    TaskSpec,
    ThreadEngine,
    run_shutdown,
)


def main() -> None:
    engine = ThreadEngine(workers=4, cleanup_workers=2)
    specs = [
        # Stuck tasks are submitted first so they actually occupy workers.
        TaskSpec("stuck0", Policy.CHECKPOINT, (1.0,), stuck=True),
        TaskSpec("stuck1", Policy.CHECKPOINT, (1.0,), stuck=True),
        TaskSpec("long-ck", Policy.CHECKPOINT, (0.5,) * 20, cleanup_cost=0.05),
        TaskSpec("long-rb", Policy.ROLLBACK, (0.5,) * 20, cleanup_cost=0.05,
                 rollback_step_cost=0.01),
        TaskSpec("quick1", Policy.CHECKPOINT, (0.05, 0.05, 0.05),
                 cleanup_cost=0.02),
        TaskSpec("quick2", Policy.CHECKPOINT, (0.05, 0.05, 0.05),
                 cleanup_cost=0.02),
        TaskSpec("quick3", Policy.CHECKPOINT, (0.05, 0.05, 0.05),
                 cleanup_cost=0.02),
    ]
    for s in specs:
        engine.submit(s)

    time.sleep(0.1)  # let the load become in-flight before the signal

    config = ShutdownConfig(
        drain_timeout=0.6,
        cleanup_timeout=0.6,
        force_wait=0.4,
        punctuality_slack=0.2,  # OS scheduling jitter on a real clock
    )
    print(f"shutdown signal received; budget = {config.budget:.2f}s", flush=True)
    start = time.monotonic()
    report = run_shutdown(engine, config)
    elapsed = time.monotonic() - start

    print(report.to_markdown(), flush=True)
    payload = report.to_dict()
    payload["elapsed"] = elapsed
    payload["budget"] = config.budget
    print("REPORT " + json.dumps(payload), flush=True)

    # Hard guarantee: terminate the process immediately. Stuck daemon threads
    # are killed with it; the release pipeline is unblocked on schedule.
    os._exit(42)


if __name__ == "__main__":
    main()

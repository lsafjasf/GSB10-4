"""Demo: simulated workload producing an attributed leak report.

Scenarios: normal release, release on exception path, partial leak,
exception-path leak, double release, refcounted shared connection,
and a legitimately long-lived pool handle (false-positive exclusion).

Run: python3 demo.py
"""

from leakdet import LeakTracker


def worker_normal(t):
    with t.acquire(kind="connection", label="db:worker_normal"):
        pass  # use the connection; released by the context manager


def worker_exception_safe(t):
    try:
        with t.acquire(kind="connection", label="db:worker_exception_safe"):
            raise RuntimeError("query failed")
    except RuntimeError:
        pass  # released in __exit__ despite the exception


def worker_leaky(t):
    t.acquire(kind="connection", label="db:worker_leaky")  # never released


def worker_exception_leaky(t):
    h = t.acquire(kind="connection", label="db:worker_exception_leaky")
    try:
        raise IOError("disk gone")
    except IOError:
        return  # BUG: forgot h.release() in the error path
    h.release()


def worker_double_free(t):
    h = t.acquire(kind="fd", label="fd:logfile")
    h.release()
    h.release(note="cleanup again")  # double release


def worker_shared(t):
    h = t.acquire(kind="connection", label="db:shared")
    h.retain()      # two logical owners
    h.release()     # owner 1 done -> still alive (refcount 1)
    h.release()     # owner 2 done -> freed


def main():
    t = LeakTracker(stack_depth=6)

    for _ in range(50):
        worker_normal(t)
    for _ in range(10):
        worker_exception_safe(t)
    for _ in range(3):
        worker_leaky(t)
    worker_exception_leaky(t)
    worker_double_free(t)
    worker_shared(t)

    pool = t.acquire(kind="connection", label="pool:main")  # legit long-lived

    print("--- check_leaks() with no threshold, pool excluded as false positive ---")
    report = t.check_leaks(exclude=lambda info: (info.label or "").startswith("pool:"))
    print(report.format())
    print()
    print("--- release log tail (normal + exceptional releases are both recorded) ---")
    for rec in t.release_log()[-4:]:
        print(f"  hid={rec['hid']} kind={rec['kind']} site={rec['site']} "
              f"note={rec['note']} held_for={rec['held_for']*1e3:.2f}ms")
    print()
    acc = t.accounting()
    print("--- accounting ---")
    print(f"  acquired={acc['acquired']} released={acc['released']} "
          f"live={acc['live']} balanced={acc['balanced']}")
    pool.release()


if __name__ == "__main__":
    main()

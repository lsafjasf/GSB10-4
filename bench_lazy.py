"""Benchmark: 1,000,000 get() calls, locked vs lock-free fast path.

Run:  python3 bench_lazy.py
"""

import statistics
import threading
import time

from lazyinit import Lazy, LazyLocked

N = 1_000_000
REPEAT = 5


def bench_single(impl, n=N):
    impl.get()  # warm up / initialize
    start = time.perf_counter()
    for _ in range(n):
        impl.get()
    return time.perf_counter() - start


def bench_multi(cls, threads, n=N):
    impl = cls(lambda: object())
    impl.get()
    barrier = threading.Barrier(threads)
    per_thread = n // threads

    def worker():
        barrier.wait()
        for _ in range(per_thread):
            impl.get()

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    start = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return time.perf_counter() - start


def main():
    print(f"Python benchmark: {N:,} get() calls per measurement, "
          f"median of {REPEAT} runs\n")

    print("== single thread ==")
    for name, cls in [("Lazy (lock-free fast path)", Lazy),
                      ("LazyLocked (lock every call)", LazyLocked)]:
        samples = [bench_single(cls(lambda: 0)) for _ in range(REPEAT)]
        med = statistics.median(samples)
        print(f"  {name:34s} {med*1e3:8.1f} ms   "
              f"({med/N*1e9:6.1f} ns/get, {N/med/1e6:6.2f} M gets/s)")

    # Baseline: plain attribute read, for scale.
    class Box:
        v = 0
    box = Box()
    samples = []
    for _ in range(REPEAT):
        start = time.perf_counter()
        for _ in range(N):
            box.v
        samples.append(time.perf_counter() - start)
    med = statistics.median(samples)
    print(f"  {'plain attribute read (baseline)':34s} {med*1e3:8.1f} ms   "
          f"({med/N*1e9:6.1f} ns/get)")

    print("\n== 8 threads x 125,000 gets ==")
    for name, cls in [("Lazy (lock-free fast path)", Lazy),
                      ("LazyLocked (lock every call)", LazyLocked)]:
        samples = [bench_multi(cls, 8) for _ in range(REPEAT)]
        med = statistics.median(samples)
        print(f"  {name:34s} {med*1e3:8.1f} ms   "
              f"({med/N*1e9:6.1f} ns/get, {N/med/1e6:6.2f} M gets/s)")


if __name__ == "__main__":
    main()

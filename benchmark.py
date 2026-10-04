"""Benchmark: 1000 subscribers x 100,000 publishes.

Measures wall time and peak memory (tracemalloc Python-heap peak and
process peak RSS via resource). Run:  python3 benchmark.py
"""

import gc
import resource
import sys
import time
import tracemalloc

from pubsub import EventBus

SUBSCRIBERS = 1000
PUBLISHES = 100_000
TOPIC = "bench"


def main() -> None:
    trace = "--no-trace" not in sys.argv
    bus = EventBus()
    sink = 0

    def make_handler(i):
        def handler(event, i=i):
            nonlocal_sink[0] += event + i  # minimal realistic work
        return handler

    nonlocal_sink = [0]
    for i in range(SUBSCRIBERS):
        bus.subscribe(TOPIC, make_handler(i), name=f"sub-{i}")

    gc.collect()
    if trace:
        tracemalloc.start()
    start = time.perf_counter()

    total_delivered = 0
    total_errors = 0
    for event in range(PUBLISHES):
        report = bus.publish(TOPIC, event)
        total_delivered += report.delivered_count
        total_errors += report.failed_count

    elapsed = time.perf_counter() - start
    py_peak = 0
    if trace:
        _, py_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    rss_peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    sink = nonlocal_sink[0]

    calls = SUBSCRIBERS * PUBLISHES
    print(f"subscribers        : {SUBSCRIBERS}")
    print(f"publishes          : {PUBLISHES:,}")
    print(f"handler calls      : {calls:,}")
    print(f"delivered (sum)    : {total_delivered:,}")
    print(f"errors (sum)       : {total_errors:,}")
    print(f"wall time          : {elapsed:.3f} s")
    print(f"per publish        : {elapsed / PUBLISHES * 1e6:.2f} us")
    print(f"per handler call   : {elapsed / calls * 1e9:.1f} ns")
    print(f"throughput         : {calls / elapsed / 1e6:.2f} M calls/s")
    print(f"py heap peak       : {py_peak / 1024 / 1024:.2f} MiB (tracemalloc)")
    print(f"process RSS peak   : {rss_peak_kb / 1024:.2f} MiB (ru_maxrss)")
    print(f"sink checksum      : {sink}")


if __name__ == "__main__":
    main()

"""Benchmark: one key change in a 1MB and a 16MB config. Run: python3 bench.py"""

import os
import resource
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cfgrewrite import get_value, set_value

def make_config(path, target_size):
    chunks = ["# benchmark config\n", "# generated, deterministic\n"]
    size = sum(len(c) for c in chunks)
    i = 0
    while size < target_size - 200:
        if i % 200 == 0:
            line = "[section%04d]\n" % (i // 200)
        elif i % 50 == 0:
            line = "# comment block %d\n" % i
        else:
            line = "key%08d = value-%08d-padding-padding-padding  # c%d\n" % (i, i, i)
        chunks.append(line)
        size += len(line)
        i += 1
    last_section = "section%04d" % ((i - 1) // 200)
    chunks.append("bench_target = old-value-0000000000000000000000000000\n")
    with open(path, "w") as fh:
        fh.write("".join(chunks))
    return os.path.getsize(path), last_section

def bench_one(path, size, section):
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    tracemalloc.start()
    t0 = time.perf_counter()
    changed = set_value(path, "bench_target",
                        "new-value-1111111111111111111111111111", section=section)
    dt = time.perf_counter() - t0
    _, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    assert changed
    assert get_value(path, "bench_target", section=section) == \
        "new-value-1111111111111111111111111111"
    # idempotent second run
    t0 = time.perf_counter()
    changed2 = set_value(path, "bench_target",
                         "new-value-1111111111111111111111111111", section=section)
    dt2 = time.perf_counter() - t0
    assert not changed2

    print("size=%8d bytes | change: %7.2f ms | python peak: %7.2f MB | "
          "process maxRSS +%6.2f MB | idempotent re-run: %6.2f ms (no write)"
          % (size, dt * 1e3, py_peak / 1e6, (rss_after - rss_before) / 1024,
             dt2 * 1e3))

def main():
    tmpdir = tempfile.mkdtemp(prefix="cfgrewrite-bench-")
    for target in (1 * 1024**2, 16 * 1024**2):
        path = os.path.join(tmpdir, "bench-%d.conf" % target)
        size, section = make_config(path, target)
        bench_one(path, size, section)
        os.unlink(path)

if __name__ == "__main__":
    main()

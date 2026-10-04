"""Benchmark: one edit on ~1MB and ~16MB config files.

Each size is measured in a fresh subprocess so the peak RSS reading is
clean. Reported: wall time of the edit, child peak RSS (incl. interpreter
baseline), and tracemalloc Python-heap peak (separate run, since
tracemalloc itself slows execution).
"""

import os
import resource
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))

CHILD_TIMED = r"""
import sys, time
sys.path.insert(0, %r)
import config_rewrite as cr
path = sys.argv[1]
t0 = time.perf_counter()
rep = cr.rewrite_value(path, "host", "new-value-9", section="target")
t1 = time.perf_counter()
assert rep["changed"]
rep2 = cr.rewrite_value(path, "host", "new-value-9", section="target")
assert not rep2["changed"]
print("%%.2f" %% ((t1 - t0) * 1000))
""" % HERE

CHILD_HEAP = r"""
import sys, tracemalloc
sys.path.insert(0, %r)
import config_rewrite as cr
tracemalloc.start()
cr.rewrite_value(sys.argv[1], "host", "new-value-9", section="target")
_, peak = tracemalloc.get_traced_memory()
print(peak)
""" % HERE


def generate(path, target_size):
    """Config with many sections/keys; the edited key sits near the end."""
    with open(path, "wb") as f:
        f.write(b"# generated benchmark config\n\n")
        written = 28
        i = 0
        while written < target_size - 200:
            if i % 200 == 0:
                sec = b"[section%05d]\n" % (i // 200)
                f.write(sec)
                written += len(sec)
            line = b"key%08d = value-%08d  # row %d\n" % (i, i, i)
            f.write(line)
            written += len(line)
            i += 1
        f.write(b"[target]\n")
        f.write(b"host = old-value  # edit me\n")
        f.write(b"port = 8080\n")


def bench(path, label):
    size = os.path.getsize(path)
    # timed run, no instrumentation
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    out = subprocess.run(
        [sys.executable, "-c", CHILD_TIMED, path],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    edit_ms = float(out)
    # heap-peak run (tracemalloc slows execution; time is not used)
    py_peak = int(subprocess.run(
        [sys.executable, "-c", CHILD_HEAP, path],
        capture_output=True, text=True, check=True,
    ).stdout.strip())
    print("=== %s (%d bytes) ===" % (label, size))
    print("  edit wall time      : %.1f ms (first edit; second edit is a no-op)"
          % edit_ms)
    print("  child peak RSS      : %.1f MB (ru_maxrss, incl. interpreter)"
          % (after / 1024))
    print("  python heap peak    : %.1f MB (tracemalloc, untimed)"
          % (py_peak / 1e6))
    print()


def main():
    tmp = tempfile.mkdtemp(prefix="bench_cfg_")
    try:
        for label, size in (("1MB", 1_000_000), ("16MB", 16_000_000)):
            path = os.path.join(tmp, "bench_%s.ini" % label)
            generate(path, size)
            bench(path, label)
            os.unlink(path)
    finally:
        os.rmdir(tmp)


if __name__ == "__main__":
    main()

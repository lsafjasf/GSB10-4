"""Benchmark: split archives of up to 1,000,000 lines; report time + memory.

Usage: python3 tools/benchmark.py [lines ...]   (default: 1_000_000)
Memory must stay ~constant as the message count grows (streaming proof).
"""
import os
import resource
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mailsplit import SplitRule, split_archive  # noqa: E402

MARKER = b"----MAIL-SEPARATOR----"
LINES_PER_MSG = 50  # marker(1) + 6 headers + blank + 41 body + delimiter blank


def generate(path, total_lines):
    n_msg = total_lines // LINES_PER_MSG
    body_line = b"The quick brown fox jumps over the lazy dog. 0123456789.\n"
    with open(path, "wb") as f:
        for i in range(n_msg):
            f.write(MARKER + b"\n")
            f.write(b"From: user%d@example.com\n" % i)
            f.write(b"To: peer%d@example.com\n" % i)
            f.write(b"Date: Tue, 07 Oct 2026 09:00:00 +0800\n")
            f.write(b"Subject: archive message number %d "
                    b"with a somewhat long folded\n subject line\n" % i)
            f.write(b"Message-ID: <%d@archive>\n" % i)
            f.write(b"Content-Type: text/plain; charset=utf-8\n")
            f.write(b"\n")
            for j in range(41):
                if j == 20 and i % 7 == 0:
                    # marker-like body line, mid-paragraph (must not split)
                    f.write(MARKER + b"\n")
                else:
                    f.write(body_line)
            f.write(b"\n")  # delimiter blank line before the next marker
    return n_msg


def bench(path):
    tracemalloc.start()
    t0 = time.perf_counter()
    n = headers = 0
    with open(path, "rb") as f:
        for msg in split_archive(f, SplitRule(marker=MARKER)):
            n += 1
            headers += len(msg.headers)
    dt = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_hw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return n, headers, dt, peak, rss_hw


def main():
    sizes = [int(x) for x in sys.argv[1:]] or [1_000_000]
    print("%-12s %-10s %-12s %-10s %-16s %-18s"
          % ("lines", "messages", "time(s)", "MB/s", "py-peak(KiB)", "rss-hwm(KiB)"))
    for total in sizes:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "archive.txt")
            n_msg = generate(path, total)
            size_mb = os.path.getsize(path) / 1e6
            n, headers, dt, peak, drss = bench(path)
            assert n == n_msg, (n, n_msg)
            print("%-12d %-10d %-12.3f %-10.1f %-16d %-18d"
                  % (total, n, dt, size_mb / dt, peak // 1024, drss))


if __name__ == "__main__":
    main()

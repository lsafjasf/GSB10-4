"""Benchmark: split a large archive, measure wall time and peak RSS.

Memory must stay flat as the message count grows, because mailsplit.split is
a generator and only the current message is resident.

Usage: python3 bench.py [lines]   (default 1_000_000)
"""

import os
import resource
import sys
import tempfile
import time

import mailsplit

BODY_LINE = b"The quick brown fox jumps over the lazy dog. 0123456789.\n"
HEADER_BLOCK = (
    b"From: Bench User <bench@example.com>\n"
    b"To: archive@example.com\n"
    b"Subject: benchmark message\n"
    b"Content-Type: text/plain; charset=utf-8\n"
    b"\n"
)


def build_archive(path: str, total_lines: int) -> int:
    """Write an archive of ~total_lines lines; return actual line count."""
    per_msg = 5 + 45  # 5 header lines + 45 body lines
    written = 0
    n = 0
    with open(path, "wb") as fh:
        while written < total_lines:
            fh.write(b"From bench%d@example.com Mon Oct  5 09:00:00 2026\n" % n)
            fh.write(HEADER_BLOCK)
            fh.write(BODY_LINE * 45)
            written += per_msg + 1
            n += 1
    return written


def peak_rss_mb() -> float:
    # ru_maxrss is KiB on Linux.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def run(total_lines: int) -> None:
    fd, path = tempfile.mkstemp(suffix=".mbox")
    os.close(fd)
    try:
        lines = build_archive(path, total_lines)
        size_mb = os.path.getsize(path) / 1e6
        rss_before = peak_rss_mb()

        start = time.perf_counter()
        count = 0
        body_bytes = 0
        header_fields = 0
        with open(path, "rb") as fh:
            for msg in mailsplit.split(fh):
                count += 1
                body_bytes += len(msg.body())
                header_fields += len(msg.headers())
        elapsed = time.perf_counter() - start
        rss_after = peak_rss_mb()

        print(f"lines={lines:>9,}  file={size_mb:7.1f} MB  messages={count:>7,}")
        print(f"  split time      : {elapsed:6.2f} s  "
              f"({lines / elapsed / 1e6:.2f} M lines/s)")
        print(f"  peak RSS        : {rss_after:7.1f} MB (delta {rss_after - rss_before:+.1f})")
        print(f"  body bytes read : {body_bytes / 1e6:7.1f} MB, "
              f"header fields: {header_fields:,}")
    finally:
        os.unlink(path)


if __name__ == "__main__":
    targets = [int(sys.argv[1])] if len(sys.argv) > 1 else [200_000, 1_000_000]
    for t in targets:
        run(t)

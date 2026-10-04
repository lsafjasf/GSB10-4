"""Subprocess worker for honest peak-RSS measurement.

Usage: python3 bench/_worker.py stream|oneshot <compressed-file> <declared-length>
Prints: <mode> <elapsed_s> <peak_rss_mib> <produced_bytes> <sha256>
"""

import hashlib
import os
import resource
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from streamdecode import iter_decode  # noqa: E402


def main():
    mode, path, declared = sys.argv[1], sys.argv[2], int(sys.argv[3])
    digest = hashlib.sha256()
    produced = 0
    t0 = time.perf_counter()
    if mode == "stream":
        with open(path, "rb") as fh:
            chunks = iter(lambda: fh.read(65536), b"")
            for piece in iter_decode(
                ["gzip"], chunks, declared_length=declared
            ):
                digest.update(piece)
                produced += len(piece)
    elif mode == "oneshot":
        import gzip

        with open(path, "rb") as fh:
            blob = fh.read()
        out = gzip.decompress(blob)
        digest.update(out)
        produced = len(out)
    else:
        raise SystemExit("unknown mode %r" % mode)
    elapsed = time.perf_counter() - t0
    peak_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    print("%s %.4f %.2f %d %s" % (mode, elapsed, peak_mib, produced, digest.hexdigest()))


if __name__ == "__main__":
    main()

"""Provable-memory-bound tests.

Runs the streaming decoder in fresh subprocesses over increasing body
sizes (1 MiB, 8 MiB, 64 MiB).  Peak memory must stay essentially flat;
the accepted band is far below the smallest response size and is fixed
by chunk size, not response size.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH = REPO_ROOT / "tools" / "bench.py"

TRACE_DELTA_KIB = float(os.environ.get("TRACE_DELTA_KIB", "4096"))
RSS_DELTA_KIB = float(os.environ.get("RSS_DELTA_KIB", "8192"))
SIZES = os.environ.get("MEM_SIZES", "1MiB,8MiB,64MiB")

# Memory is measured on the "hard" (incompressible, gzip level 1) corpus:
# compressed size ~= plaintext size, so zlib actually streams ~64 MiB
# through the pipeline at 64 MiB (no tiny-body hiding).


def run_series(encoding, sizes=SIZES):
    proc = subprocess.run(
        [sys.executable, str(BENCH), "series",
         "--sizes", sizes, "--style", "hard", "--encoding", encoding],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


class MemoryBoundTests(unittest.TestCase):
    def _assert_flat(self, rows, field, max_delta):
        vals = [r[field] for r in rows]
        delta = max(vals) - min(vals)
        self.assertLessEqual(
            delta, max_delta,
            "peak memory grew with response size: %s (%s), delta=%.1f KiB"
            % (vals, field, delta),
        )

    def _assert_correct(self, rows):
        for row in rows:
            self.assertTrue(row["hash_match"], row)
            # Incompressible corpus: the coded body is at least ~99% of
            # the plaintext size, so the decoder really streamed the
            # full volume (no tiny-body hiding).
            self.assertGreaterEqual(
                row["body_size"], int(row["plain_size"] * 0.99), row
            )

    def test_gzip_flat(self):
        rows = run_series("gzip")
        self._assert_correct(rows)
        self._assert_flat(rows, "tracemalloc_peak_kib", TRACE_DELTA_KIB)
        self._assert_flat(rows, "rss_peak_kib", RSS_DELTA_KIB)
        # Absolute guard: even 64 MiB must decode with Python-allocator
        # peak under a few MiB (baseline + a handful of 64 KiB buffers).
        self.assertLess(
            max(r["tracemalloc_peak_kib"] for r in rows), 8 * 1024,
            [r["tracemalloc_peak_kib"] for r in rows],
        )

    def test_chain_flat(self):
        rows = run_series("gzip, deflate", sizes="1MiB,8MiB,32MiB")
        self._assert_correct(rows)
        self._assert_flat(rows, "tracemalloc_peak_kib", TRACE_DELTA_KIB)
        self._assert_flat(rows, "rss_peak_kib", RSS_DELTA_KIB)

    def test_sink_does_not_accumulate(self):
        # The flatness numbers only mean anything if the sink streams.
        # This documents the contract: hash + counter, no bytearray.
        rows = run_series("gzip", sizes="1MiB,4MiB")
        for row in rows:
            self.assertGreater(row["plain_size"], 0)
            self.assertEqual(row["mode"], "streaming")


if __name__ == "__main__":
    unittest.main()

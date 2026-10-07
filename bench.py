"""Benchmark: encode + decode 1,000,000 header blocks.

Run:  python3 bench.py [block_count]
Writes docs/benchmark.txt and prints the same report.
Only the Python standard library is used.
"""

import os
import random
import resource
import sys
import time
import tracemalloc

from dyntable import Decoder, Encoder

BLOCK_COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
HEADERS_PER_BLOCK = 6
TABLE_CAPACITY = 4096

NAME_POOL = (
    ":method", ":path", ":scheme", ":status",
    "content-type", "content-length", "cache-control",
    "user-agent", "accept", "accept-encoding",
    "x-request-id", "x-session",
)


def build_value_pool():
    rng = random.Random(7)
    common = [
        "GET", "/index.html", "https", "200",
        "text/html", "application/json", "max-age=60",
        "gzip", "br", "Mozilla/5.0", "*/*",
    ]
    tokens = [
        "%s-%04d" % (rng.choice(["tok", "sess", "rid", "v"]), rng.randrange(1 << 16))
        for _ in range(489)
    ]
    return common + tokens


VALUE_POOL = build_value_pool()


def gen_headers(rng):
    headers = []
    for i in range(HEADERS_PER_BLOCK):
        name = NAME_POOL[i % len(NAME_POOL)]
        if rng.random() < 0.85:
            value = rng.choice(VALUE_POOL)
        else:
            value = "u%d-%d" % (rng.randrange(1 << 32), i)
        headers.append((name, value))
    return headers


def main():
    enc = Encoder(TABLE_CAPACITY)
    dec = Decoder(TABLE_CAPACITY)
    rng = random.Random(20261007)

    tracemalloc.start()
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    wall_start = time.perf_counter()

    encode_secs = 0.0
    decode_secs = 0.0
    wire_bytes = 0
    errors = 0

    for _ in range(BLOCK_COUNT):
        headers = gen_headers(rng)

        t0 = time.perf_counter()
        block = enc.encode_header_block(headers)
        chunk_bytes = enc.flush_stream_chunk()
        encode_secs += time.perf_counter() - t0
        wire_bytes += len(block) + len(chunk_bytes)

        t0 = time.perf_counter()
        try:
            dec.feed_stream_chunk(chunk_bytes)
            decoded = dec.decode_header_block(block)
            if decoded != headers:
                errors += 1
        except Exception:  # noqa: BLE001 - benchmark must surface errors
            errors += 1
        decode_secs += time.perf_counter() - t0

    wall_secs = time.perf_counter() - wall_start
    _, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    header_count = BLOCK_COUNT * HEADERS_PER_BLOCK
    state_equal = enc.table.snapshot() == dec.table.snapshot()

    report = [
        "== Dynamic table benchmark: %d header blocks ==" % BLOCK_COUNT,
        "headers per block      : %d (%d header fields total)"
        % (HEADERS_PER_BLOCK, header_count),
        "dynamic table capacity : %d bytes" % TABLE_CAPACITY,
        "workload               : 12 names, 500-value hot pool "
        "(85% hits), 15% unique values",
        "",
        "encode time            : %.3f s (block + stream chunk)"
        % encode_secs,
        "decode time            : %.3f s (chunk apply + block decode)"
        % decode_secs,
        "total wall time        : %.3f s" % wall_secs,
        "encode throughput      : %.0f blocks/s (%.0f fields/s)"
        % (BLOCK_COUNT / encode_secs, header_count / encode_secs),
        "decode throughput      : %.0f blocks/s (%.0f fields/s)"
        % (BLOCK_COUNT / decode_secs, header_count / decode_secs),
        "python object peak     : %.2f MiB (tracemalloc)"
        % (py_peak / 1048576),
        "process RSS peak       : %.2f MiB (ru_maxrss, %.1f MiB "
 "over baseline)"
        % (rss_after / 1024, (rss_after - rss_before) / 1024),
        "wire volume            : %.2f MiB (%.1f bytes/block)"
        % (wire_bytes / 1048576, wire_bytes / BLOCK_COUNT),
        "decode mismatches      : %d" % errors,
        "end states identical   : %s" % state_equal,
        "table after run        : size=%d inserts=%d dropped=%d"
        % (dec.table.size, dec.table.insert_count,
           dec.table.dropped_count),
        "",
        "environment            : Python %s"
        % sys.version.split()[0],
    ]
    text = "\n".join(report)
    print(text)

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "docs")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "benchmark.txt"),
              "w", encoding="utf-8") as f:
        f.write(text + "\n")

    if errors or not state_equal:
        sys.exit(1)


if __name__ == "__main__":
    main()

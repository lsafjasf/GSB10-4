"""Differential data: streaming decode vs one-shot stdlib oracle.

For each (chain, payload size, read chunk size, payload kind) triple the
script records SHA-256 of the original payload, the one-shot oracle output
(gzip.decompress / zlib.decompress / bz2.decompress applied in reverse
declaration order) and the streaming output. All three hashes must match.

Usage (repo root):
    python3 scripts/differential.py
Writes reports/differential.txt.
"""

import bz2
import gzip
import hashlib
import os
import random
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from streamdecode import iter_decode  # noqa: E402


def raw_deflate(data, level=6):
    co = zlib.compressobj(level, zlib.DEFLATED, -zlib.MAX_WBITS)
    return co.compress(data) + co.flush()


ENCODERS = {
    "gzip": gzip.compress,
    "deflate": lambda b: zlib.compress(b, 6),
    "deflate-raw": raw_deflate,
    "bzip2": bz2.compress,
}


def apply_chain(chain, data):
    for name in chain:
        data = ENCODERS[name](data) if name != "identity" else data
    return data


def oracle(chain, blob):
    for name in reversed(chain):
        if name == "gzip":
            blob = gzip.decompress(blob)
        elif name == "deflate":
            try:
                blob = zlib.decompress(blob)
            except zlib.error:
                blob = zlib.decompress(blob, -zlib.MAX_WBITS)
        elif name == "deflate-raw":
            blob = zlib.decompress(blob, -zlib.MAX_WBITS)
        elif name == "bzip2":
            blob = bz2.decompress(blob)
        # identity: unchanged
    return blob


def payload(size, mode, seed):
    if size == 0:
        return b""
    rng = random.Random(seed)
    if mode == "random":
        return rng.randbytes(size)
    block = b"The quick brown fox jumps over the lazy dog. " * 64 + rng.randbytes(4096)
    out = bytearray()
    while len(out) < size:
        out += block
    return bytes(out[:size])


def chunks(data, n):
    for i in range(0, len(data), n):
        yield data[i : i + n]


def main():
    chains = [
        ["gzip"],
        ["deflate"],
        ["deflate-raw"],
        ["bzip2"],
        ["gzip", "deflate"],
        ["deflate", "gzip"],
        ["bzip2", "deflate", "gzip"],
        ["identity", "gzip"],
    ]
    sizes = [0, 1, 3, 7, 1000, 4096, 65_537, (1 << 20) + 3, (8 << 20) + 5]
    rows = []
    failures = 0
    for chain in chains:
        for size in sizes:
            for mode in ("mixed", "random"):
                data = payload(size, mode, seed=size ^ hash(tuple(chain)) & 0xFFFF)
                blob = apply_chain(chain, data)
                one_shot = oracle(chain, blob)
                chunk_size = 1 if size <= 7 else (4096 if size < 65_536 else 1 << 20)
                # the public 'deflate' decoder auto-detects raw deflate
                stream_chain = ["deflate" if n == "deflate-raw" else n for n in chain]
                streamed = b"".join(iter_decode(stream_chain, chunks(blob, chunk_size)))
                h_orig = hashlib.sha256(data).hexdigest()
                h_once = hashlib.sha256(one_shot).hexdigest()
                h_stream = hashlib.sha256(streamed).hexdigest()
                ok = h_orig == h_once == h_stream
                failures += not ok
                rows.append(
                    (
                        "OK " if ok else "FAIL",
                        ",".join(chain),
                        mode,
                        size,
                        len(blob),
                        chunk_size,
                        h_stream[:16],
                    )
                )
    header = (
        "streamdecode differential data (streaming vs one-shot stdlib oracle)\n"
        "encodings are listed in declared (= application) order\n"
        "status  chain                         mode    plaintext  compressed  chunk  sha256[:16]\n"
        + "-" * 104
    )
    body = "\n".join(
        "%-4s  %-29s  %-6s  %9d  %10d  %5d  %s" % r for r in rows
    )
    footer = "-" * 104 + "\n%d cases, %d failures\n" % (len(rows), failures)
    report = header + "\n" + body + "\n" + footer
    print(report)
    out = os.path.join(ROOT, "reports", "differential.txt")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(report)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

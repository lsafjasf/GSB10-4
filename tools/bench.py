#!/usr/bin/env python3
"""Benchmark + memory instrumentation for the streaming decoder.

Subcommands
-----------
gen       : stream-generate an encoded corpus file (no body ever held
            in memory); emits sidecar JSON with plaintext size/hash.
measure   : stream-decode a corpus file, print JSON timing/peak-memory.
series    : generate sizes, run each `measure` in a fresh subprocess,
            print a JSON table.

Only Python standard library is used.
"""

import argparse
import gzip
import hashlib
import json
import os
import random
import resource
import subprocess
import sys
import tempfile
import time
import tracemalloc
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codec import StreamingDecoder, decode_chain, encode_chain  # noqa: E402
from codec.negotiation import parse_content_encoding  # noqa: E402

_CHUNK = 1 << 20


# ---------------------------------------------------------------- corpus

def _plain_chunks(style, size, seed):
    rng = random.Random(seed)
    words = (b"lorem", b"ipsum", b"dolor", b"sit", b"amet",
             b"consectetur", b"adipiscing", b"elit")
    remaining = size
    line_no = 0
    while remaining:
        n = min(_CHUNK, remaining)
        remaining -= n
        if style == "hard":
            yield rng.randbytes(n)
            continue
        parts = []
        made = 0
        while made < n:
            line_no += 1
            line = b"%010d %s %s %s %s\n" % (
                line_no, words[line_no % len(words)],
                words[(line_no * 3) % len(words)],
                words[(line_no * 7) % len(words)],
                words[(line_no * 13) % len(words)],
            )
            if made + len(line) > n:
                line = line[: n - made]
            parts.append(line)
            made += len(line)
        yield b"".join(parts)


class _ZlibWriter:
    """File-like wrapper producing a zlib (deflate coding) stream."""

    def __init__(self, out, level=1, wbits=15):
        self._out = out
        self._co = zlib.compressobj(level, zlib.DEFLATED, wbits)

    def write(self, data):
        out = self._co.compress(data)
        if out:
            self._out.write(out)
        return len(data)

    def close(self):
        out = self._co.flush()
        if out:
            self._out.write(out)


def _open_writer(tokens, path, level):
    raw = open(path, "wb")
    stack = [raw]
    writer = raw
    # First-listed coding was applied first, so it must be the innermost
    # writer; wrap from last token back to first.
    for token in reversed(tokens):
        if token == "gzip":
            writer = gzip.GzipFile(
                filename="", mode="wb", fileobj=writer,
                mtime=0, compresslevel=level,
            )
            stack.append(writer)
        elif token == "deflate":
            writer = _ZlibWriter(writer, level=level, wbits=15)
            stack.append(writer)
        elif token == "identity":
            continue
        else:
            raise ValueError("cannot generate coding %r" % token)
    return writer, stack


def generate(path, size, style, encoding, level=1):
    tokens = parse_content_encoding(encoding)
    writer, stack = _open_writer(tokens, path, level)
    hasher = hashlib.sha256()
    try:
        for chunk in _plain_chunks(style, size, seed=20261005):
            hasher.update(chunk)
            writer.write(chunk)
    finally:
        for w in reversed(stack[1:]):
            w.close()
        stack[0].close()
    return {
        "file": str(path),
        "encoding": encoding,
        "style": style,
        "plain_size": size,
        "body_size": os.path.getsize(path),
        "plain_sha256": hasher.hexdigest(),
    }


# --------------------------------------------------------------- measure

def measure(path, encoding, in_chunk, out_chunk, reference=False):
    path = Path(path)
    hasher = hashlib.sha256()
    plain_size = 0
    gc_collect()
    tracemalloc.start()
    start = time.perf_counter()
    if reference:
        data = path.read_bytes()
        out = decode_chain(encoding, data)
        hasher.update(out)
        plain_size = len(out)
        del data, out
    else:
        with open(path, "rb", buffering=0) as raw:
            def sink(chunk):
                nonlocal plain_size
                plain_size += len(chunk)
                hasher.update(chunk)

            decoder = StreamingDecoder(
                encoding, sink, in_chunk=in_chunk, out_chunk=out_chunk
            )
            while True:
                block = raw.read(in_chunk)
                if not block:
                    break
                decoder.feed(block)
            decoder.finish()
    elapsed = time.perf_counter() - start
    traced_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    rss_peak_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "mode": "one-shot" if reference else "streaming",
        "encoding": encoding,
        "body_size": path.stat().st_size,
        "plain_size": plain_size,
        "seconds": round(elapsed, 4),
        "throughput_mib_s": round(
            plain_size / (1024 * 1024) / elapsed if elapsed else 0.0, 2
        ),
        "tracemalloc_peak_kib": round(traced_peak / 1024, 1),
        "rss_peak_kib": rss_peak_kib,
        "sha256": hasher.hexdigest(),
        "in_chunk_kib": in_chunk // 1024,
        "out_chunk_kib": out_chunk // 1024,
    }


def gc_collect():
    import gc
    gc.collect()


def parse_size(text):
    units = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3,
             "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3}
    t = text.strip().upper()
    for suffix in ("MIB", "GIB", "KIB", "MB", "GB", "KB", "B"):
        if t.endswith(suffix):
            return int(float(t[: -len(suffix)]) * units[suffix])
    return int(t)


# ------------------------------------------------------------------ CLI

def main(argv=None):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen")
    g.add_argument("--out", required=True)
    g.add_argument("--size", required=True)
    g.add_argument("--style", choices=("hard", "text"), default="hard")
    g.add_argument("--encoding", default="gzip")
    g.add_argument("--level", type=int, default=1)

    m = sub.add_parser("measure")
    m.add_argument("--file", required=True)
    m.add_argument("--encoding", default="gzip")
    m.add_argument("--in-chunk", default="64KiB")
    m.add_argument("--out-chunk", default="64KiB")
    m.add_argument("--reference", action="store_true")

    s = sub.add_parser("series")
    s.add_argument("--sizes", required=True)
    s.add_argument("--style", choices=("hard", "text"), default="hard")
    s.add_argument("--encoding", default="gzip")
    s.add_argument("--keep", default=None)

    args = p.parse_args(argv)

    if args.cmd == "gen":
        info = generate(args.out, parse_size(args.size), args.style,
                        args.encoding, args.level)
        print(json.dumps(info, indent=2))
        return 0

    if args.cmd == "measure":
        info = measure(
            args.file, args.encoding,
            parse_size(args.in_chunk), parse_size(args.out_chunk),
            reference=args.reference,
        )
        print(json.dumps(info, indent=2))
        return 0

    if args.cmd == "series":
        sizes = [parse_size(x) for x in args.sizes.split(",")]
        tmpdir = Path(args.keep) if args.keep else Path(
            tempfile.mkdtemp(prefix="cec-bench-")
        )
        tmpdir.mkdir(parents=True, exist_ok=True)
        rows = []
        for size in sizes:
            tag = "%s_%s" % (args.style, size)
            body = tmpdir / (tag + ".body")
            info = generate(body, size, args.style, args.encoding)
            env = dict(os.environ)
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "measure",
                 "--file", str(body), "--encoding", args.encoding],
                capture_output=True, text=True, env=env, check=True,
            )
            row = json.loads(proc.stdout)
            row["expected_sha256"] = info["plain_sha256"]
            row["hash_match"] = row["sha256"] == info["plain_sha256"]
            rows.append(row)
            if not args.keep:
                body.unlink()
        print(json.dumps(rows, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

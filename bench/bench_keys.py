"""Benchmark: 1,000,000 cache key constructions + key length distribution.

Run:  python3 bench/bench_keys.py [--n 1000000]
"""

import argparse
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cachekey import KeyBuilder


def make_corpus(rng, size):
    """Realistic mix: single-value, multi-value, mixed case, long headers."""
    corpus = []
    encodings = ["gzip", "deflate", "br", "zstd", "identity"]
    languages = ["en-US", "zh-CN", "ja-JP", "de-DE", "fr-FR"]
    medias = ["application/json", "text/html", "text/plain",
              "application/xml", "image/webp"]
    for _ in range(size):
        headers = []
        # multi-value Accept (order/casing/whitespace vary on the wire)
        n = rng.randint(1, 4)
        accept = ", ".join(
            (" " * rng.randint(0, 2)) + rng.choice(medias) for _ in range(n)
        )
        headers.append(("Accept" if rng.random() < 0.5 else "accept", accept))
        # Accept-Encoding: mixed case tokens, random order
        codings = rng.sample(encodings, rng.randint(1, 3))
        headers.append((
            "Accept-Encoding",
            ", ".join(c.upper() if rng.random() < 0.3 else c for c in codings),
        ))
        # single-value Content-Type
        if rng.random() < 0.7:
            ct = rng.choice(medias)
            if rng.random() < 0.3:
                ct += "; charset=utf-8"
            headers.append(("Content-Type", ct))
        # Accept-Language list
        if rng.random() < 0.6:
            headers.append(("Accept-Language", ", ".join(
                rng.sample(languages, rng.randint(1, 3)))))
        # raw Cookie, sometimes very long (session + analytics blobs)
        if rng.random() < 0.5:
            cookie = "sid=" + "".join(rng.choices("abcdef0123456789", k=32))
            if rng.random() < 0.05:  # 5% long-header traffic
                cookie += "; analytics=" + "".join(
                    rng.choices("abcdef0123456789", k=rng.randint(500, 4500)))
            headers.append(("Cookie", cookie))
        # auth sometimes
        if rng.random() < 0.3:
            headers.append(("Authorization", "Bearer " + "".join(
                rng.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", k=24))))
        corpus.append(headers)
    return corpus


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=1_000_000)
    parser.add_argument("--corpus", type=int, default=10_000)
    args = parser.parse_args()

    rng = random.Random(42)
    corpus = make_corpus(rng, args.corpus)
    builder = KeyBuilder()

    lengths = []
    hashed = 0
    keys_seen = set()
    start = time.perf_counter()
    for i in range(args.n):
        key = builder.build(corpus[i % len(corpus)])
        lengths.append(key.length)
        hashed += key.hashed
        keys_seen.add(key.key)
    elapsed = time.perf_counter() - start

    lengths.sort()
    n = args.n

    def pct(p):
        return lengths[min(n - 1, int(p * n))]

    # histogram buckets
    buckets = [(0, 128), (128, 256), (256, 512), (512, 1024),
               (1024, 2048), (2048, 4096), (4096, 8192), (8192, 1 << 30)]
    hist = []
    lo = 0
    for low, high in buckets:
        cnt = sum(1 for length in lengths if low <= length < high)
        hist.append((f"[{low},{high})", cnt))

    print(f"constructions        : {n:,}")
    print(f"corpus size          : {len(corpus):,} distinct requests")
    print(f"distinct keys        : {len(keys_seen):,}")
    print(f"total time           : {elapsed:.3f} s")
    print(f"throughput           : {n / elapsed:,.0f} keys/s")
    print(f"avg per construction : {elapsed / n * 1e6:.2f} us")
    print(f"hashed (overlong)    : {hashed:,} ({hashed / n:.2%})")
    print(f"key length min/mean/max : {lengths[0]} / "
          f"{statistics.mean(lengths):.1f} / {lengths[-1]}")
    print(f"key length p50/p90/p99/p99.9 : {pct(0.5)} / {pct(0.9)} / "
          f"{pct(0.99)} / {pct(0.999)}")
    print("key length histogram :")
    for label, cnt in hist:
        if cnt:
            print(f"  {label:>12} : {cnt:>9,} ({cnt / n:6.2%})")


if __name__ == "__main__":
    main()

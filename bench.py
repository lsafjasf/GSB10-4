#!/usr/bin/env python3
"""100 万次缓存键构造基准：耗时 + 键长分布。

运行：python3 bench.py [--n 1000000]
"""
import argparse
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cachekey

VARY = ["accept", "accept-encoding", "accept-language",
        "content-type", "authorization", "x-flags", "x-blob"]

ACCEPTS = ["text/html", "text/html,application/xhtml+xml",
           "application/json", "*/*", "text/html;q=0.9,application/json;q=0.8"]
ENCODINGS = ["gzip", "gzip, deflate", "gzip, deflate, br", "identity", "br"]
LANGS = ["zh-CN", "en-US,en;q=0.9", "zh-CN,zh;q=0.9,en;q=0.8", "en"]
CTYPES = ["text/html; charset=utf-8", "application/json",
          "text/html; charset=utf-8; level=1", "application/octet-stream"]
AUTHS = [None, "Bearer tok-abc123", "Basic dXNlcjpwYXNz", "Bearer tok-XYZ789"]
FLAGS = ["a,b,c", "beta", "a,b", "exp-42,beta"]
NAME_CASES = [str.lower, str.upper, str.title]


def gen_requests(n, seed=20261005):
    rng = random.Random(seed)
    blob_alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    for _ in range(n):
        headers = []
        if rng.random() < 0.95:
            headers.append(("Accept", rng.choice(ACCEPTS)))
        if rng.random() < 0.9:
            headers.append(("Accept-Encoding", rng.choice(ENCODINGS)))
        if rng.random() < 0.7:
            headers.append(("Accept-Language", rng.choice(LANGS)))
        if rng.random() < 0.6:
            headers.append(("Content-Type", rng.choice(CTYPES)))
        auth = rng.choice(AUTHS)
        if auth is not None:
            headers.append(("Authorization", auth))
        if rng.random() < 0.3:
            headers.append(("X-Flags", rng.choice(FLAGS)))
        if rng.random() < 0.02:  # 2% 超长头部
            headers.append(("X-Blob", "".join(rng.choices(blob_alphabet,
                                                          k=rng.randrange(1000, 4000)))))
        # 随机化：头部名大小写、首尾空白、重复头行
        out = []
        for name, value in headers:
            name = rng.choice(NAME_CASES)(name)
            if rng.random() < 0.3:
                value = " " * rng.randrange(1, 4) + value + "\t" * rng.randrange(0, 3)
            if rng.random() < 0.05 and "," in value:
                parts = value.split(",", 1)
                out.append((name, parts[0]))
                out.append((name, parts[1]))
            else:
                out.append((name, value))
        rng.shuffle(out)
        yield out


def percentile(sorted_vals, p):
    idx = min(len(sorted_vals) - 1, int(p / 100 * len(sorted_vals)))
    return sorted_vals[idx]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1_000_000)
    args = ap.parse_args()
    n = args.n

    print(f"构造 {n:,} 个合成请求 ...", flush=True)
    t0 = time.perf_counter()
    requests = list(gen_requests(n))
    t1 = time.perf_counter()
    print(f"请求生成耗时 {t1 - t0:.2f}s（不计入键构造耗时）", flush=True)

    lengths = []
    hash_keys = 0
    t2 = time.perf_counter()
    for headers in requests:
        k = cachekey.key(headers, VARY)
        lengths.append(len(k))
        if k.startswith("h1:"):
            hash_keys += 1
    t3 = time.perf_counter()

    total = t3 - t2
    lengths.sort()
    buckets = [(0, 64), (64, 128), (128, 256), (256, 512), (512, 771)]
    print()
    print(f"=== 100 万次键构造基准（Python {sys.version.split()[0]}，单线程）===")
    print(f"总耗时        : {total:.3f} s")
    print(f"吞吐          : {n / total:,.0f} keys/s")
    print(f"平均单次      : {total / n * 1e6:.2f} µs")
    print()
    print("键长分布（字符数）：")
    print(f"  min={lengths[0]}  p50={percentile(lengths, 50)}  "
          f"p90={percentile(lengths, 90)}  p99={percentile(lengths, 99)}  "
          f"max={lengths[-1]}  mean={statistics.mean(lengths):.1f}")
    for lo, hi in buckets:
        cnt = sum(1 for L in lengths if lo <= L < hi)
        print(f"  [{lo:>3}, {hi:>3}) : {cnt:>9,}  ({cnt / n * 100:6.2f}%)")
    print(f"  哈希键(h1:)  : {hash_keys:>9,}  ({hash_keys / n * 100:6.2f}%)")
    print(f"  可读键(v1:)  : {n - hash_keys:>9,}  ({(n - hash_keys) / n * 100:6.2f}%)")
    print()
    print(f"键长硬上界 MAX_KEY_LEN = {cachekey.MAX_KEY_LEN}，"
          f"实测 max = {lengths[-1]}，{'未越界 ✓' if lengths[-1] <= cachekey.MAX_KEY_LEN else '越界 ✗'}")


if __name__ == "__main__":
    main()

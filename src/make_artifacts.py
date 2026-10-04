"""Emit persistent 对拍/分布 artifacts into data/ and reports/.

1. data/demo_input.tsv + data/demo_input_shuffled.tsv (1,000 rows, fixed seed)
   reports/demo_sample_original.txt  / demo_sample_shuffled.txt (identical)
2. reports/shard_vs_global_10m.json: global sample vs union of 4 shards,
   exact set equality on 10,000,000 rows.
3. reports/uniformity_10m.json: 20 equal-width hash buckets over all
   10,000,000 keys (chi-square, df=19).
"""
import hashlib
import json
import math
import os
import random
import time

from sampler import Sampler

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
DATA_10M = os.path.join(ROOT, "data", "bench_10m.tsv")
SEED, RATE = 20261005, 0.01


def demo_pair():
    rows = [f"user-{i:04d}\tvalue-{i:04d}".encode() for i in range(1000)]
    shuffled = rows[:]
    random.Random(123).shuffle(shuffled)

    with open(os.path.join(ROOT, "data", "demo_input.tsv"), "wb") as f:
        f.write(b"\n".join(rows) + b"\n")
    with open(os.path.join(ROOT, "data", "demo_input_shuffled.tsv"), "wb") as f:
        f.write(b"\n".join(shuffled) + b"\n")

    s = Sampler(SEED, RATE)
    a = sorted(r for r in rows if s.keep(r))
    b = sorted(r for r in shuffled if s.keep(r))
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    with open(os.path.join(ROOT, "reports/demo_sample_original.txt"), "wb") as f:
        f.write(b"\n".join(a) + b"\n")
    with open(os.path.join(ROOT, "reports/demo_sample_shuffled.txt"), "wb") as f:
        f.write(b"\n".join(b) + b"\n")
    print(f"demo: 1000 rows, kept {len(a)}; shuffled kept {len(b)}; "
          f"sorted outputs equal: {a == b}")
    print("    sha256 original output:",
          hashlib.sha256(b"\n".join(a)).hexdigest()[:16])
    print("    sha256 shuffled output:",
          hashlib.sha256(b"\n".join(b)).hexdigest()[:16])


def big_sweep():
    n_shards = 4
    s = Sampler(SEED, RATE)
    global_kept = set()
    shard_kept = [set() for _ in range(n_shards)]
    buckets = [0] * 20
    t0 = time.perf_counter()
    with open(DATA_10M, "rb") as f:
        for idx, line in enumerate(f):
            key = line.rstrip(b"\n")
            h = s.hash64(key)
            buckets[min(19, h * 20 // (1 << 64))] += 1
            keep = h < s.threshold
            if keep:
                global_kept.add(key)
                shard_kept[idx % n_shards].add(key)
    elapsed = time.perf_counter() - t0

    union = set().union(*shard_kept)
    per_shard_counts = [len(x) for x in shard_kept]
    result = {
        "rows": 10_000_000, "seed": SEED, "rate": RATE, "shards": n_shards,
        "global_kept": len(global_kept),
        "per_shard_kept": per_shard_counts,
        "union_of_shards_kept": len(union),
        "union_equals_global": union == global_kept,
        "elapsed_s": round(elapsed, 2),
    }
    with open(os.path.join(ROOT, "reports/shard_vs_global_10m.json"), "w") as f:
        json.dump(result, f, indent=2)
    print("10m shard sweep:", json.dumps(result))

    n = sum(buckets)
    exp = n / 20
    chi2 = sum((c - exp) ** 2 / exp for c in buckets)
    max_dev = max(abs(c - exp) / exp for c in buckets)
    uni = {
        "rows": n, "buckets": 20,
        "bucket_counts": buckets,
        "expected_per_bucket": exp,
        "chi_square": round(chi2, 3),
        "crit_p_0.001_df_19": 43.820,
        "max_relative_deviation": round(max_dev, 6),
    }
    with open(os.path.join(ROOT, "reports/uniformity_10m.json"), "w") as f:
        json.dump(uni, f, indent=2)
    print("uniformity:", json.dumps(uni))


if __name__ == "__main__":
    os.chdir(os.path.join(HERE, ".."))
    demo_pair()
    big_sweep()

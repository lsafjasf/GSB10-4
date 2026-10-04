"""10,000,000-row benchmark at rate=1%.

Usage:
  python3 src/bench.py                # generate data + both sampling modes
  python3 src/bench.py --no-generate  # reuse existing data file
  python3 src/bench.py --generate-only

Each sampling mode runs in a fresh child process so ru_maxrss reflects that
mode alone.
"""
import argparse
import json
import os
import resource
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data", "bench_10m.tsv")
N_ROWS = 10_000_000
SEED = 20261005
RATE = 0.01


def generate():
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    t0 = time.perf_counter()
    with open(DATA, "wb") as f:
        write = f.write
        for i in range(N_ROWS):
            write(b"row-%09d\tpayload-%09d\n" % (i, i))
    size = os.path.getsize(DATA)
    print(f"generated {N_ROWS:,} rows, {size/1e6:.1f} MB, "
          f"{time.perf_counter()-t0:.1f}s -> {os.path.relpath(DATA, HERE + '/..')}")


def sample(mode):
    from sampler import Sampler
    sampler = Sampler(SEED, RATE)
    t0 = time.perf_counter()
    kept_count = 0
    kept = [] if mode == "collect" else None
    with open(DATA, "rb") as f:
        for line in f:
            key = line.rstrip(b"\n")
            if sampler.keep(key):
                kept_count += 1
                if kept is not None:
                    kept.append(key)
    elapsed = time.perf_counter() - t0
    peak_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps({
        "mode": mode,
        "rows": N_ROWS,
        "rate": RATE,
        "kept": kept_count,
        "expected": int(N_ROWS * RATE),
        "elapsed_s": round(elapsed, 2),
        "rows_per_s": round(N_ROWS / elapsed),
        "peak_rss_kib": peak_kib,
        "peak_rss_mib": round(peak_kib / 1024, 1),
    }))


def run_child(mode):
    out = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--mode", mode],
        cwd=HERE, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("stream", "collect"),
                    help=argparse.SUPPRESS)
    ap.add_argument("--no-generate", action="store_true")
    ap.add_argument("--generate-only", action="store_true")
    args = ap.parse_args()

    if args.mode:
        sample(args.mode)
        return

    if not args.no_generate or not os.path.exists(DATA):
        generate()
    if args.generate_only:
        return

    print(f"benchmark: {N_ROWS:,} rows, rate={RATE}, seed={SEED}")
    print("file:", os.path.relpath(DATA, HERE + "/.."),
          f"({os.path.getsize(DATA)/1e6:.1f} MB)\n")
    results = [run_child("stream"), run_child("collect")]
    for r in results:
        rel = (r["kept"] - r["expected"]) / r["expected"]
        print(f"mode={r['mode']:<7} kept={r['kept']:>7,} "
              f"(expected {r['expected']:,}, rel err {rel:+.2%})  "
              f"time={r['elapsed_s']:5.2f}s  "
              f"throughput={r['rows_per_s']:,} rows/s  "
              f"peak RSS={r['peak_rss_mib']:.1f} MiB")


if __name__ == "__main__":
    main()

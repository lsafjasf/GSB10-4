"""Benchmark: 10,000,000 keys with a 64 MiB memory budget.

Generates a deterministic key stream, dedups it with ExternalDedup, then
verifies the output byte-for-byte against `sort -u` (LC_ALL=C).

Run:  python3 bench.py
"""

import hashlib
import os
import random
import resource
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extdedup import ExternalDedup

N = 10_000_000
UNIVERSE = 8_000_000
SEED = 20261005
LIMIT = 64 * 1024 * 1024  # 64 MiB

WORK = tempfile.mkdtemp(prefix="extdedup-bench-")
KEYS_FILE = os.path.join(WORK, "keys.txt")
OUT_FILE = os.path.join(WORK, "out.sorted")
EXPECTED_FILE = os.path.join(WORK, "expected.sorted")


def gen():
    rng = random.Random(SEED)
    randrange = rng.randrange
    for _ in range(N):
        yield b"key-%09d\n" % randrange(UNIVERSE)


def write_keys_file():
    t0 = time.perf_counter()
    with open(KEYS_FILE, "wb") as fh:
        for line in gen():
            fh.write(line)
    return time.perf_counter() - t0


def run_operator():
    op = ExternalDedup(LIMIT, work_dir=WORK)
    t0 = time.perf_counter()
    with open(KEYS_FILE, "rb") as fh:
        for line in fh:
            op.add(line.rstrip(b"\n"))
    t_add = time.perf_counter()
    disk_before_merge = op.disk_bytes()

    digest = hashlib.sha1()
    count = 0
    with open(OUT_FILE, "wb") as out:
        for key in op:
            out.write(key)
            out.write(b"\n")
            digest.update(len(key).to_bytes(4, "big"))
            digest.update(key)
            count += 1
    t_merge = time.perf_counter()
    stats = dict(
        add_seconds=t_add - t0,
        merge_seconds=t_merge - t_add,
        spills=op.spill_count,
        spill_bytes=op.spill_bytes,
        merge_bytes=op.merge_bytes,
        disk_before_merge=disk_before_merge,
        unique=count,
        sha1=digest.hexdigest(),
    )
    op.close()
    return stats


def verify_with_sort():
    env = dict(os.environ, LC_ALL="C")
    t0 = time.perf_counter()
    with open(EXPECTED_FILE, "wb") as out:
        subprocess.run(
            ["sort", "-u", "-S", "1G", "--parallel=4", KEYS_FILE],
            stdout=out,
            env=env,
            check=True,
        )
    sort_seconds = time.perf_counter() - t0
    same = subprocess.run(
        ["cmp", "-s", OUT_FILE, EXPECTED_FILE]
    ).returncode == 0
    return sort_seconds, same


def main():
    rss_base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    t_gen = write_keys_file()
    keys_mb = os.path.getsize(KEYS_FILE) / (1 << 20)

    stats = run_operator()
    rss_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

    sort_seconds, same = verify_with_sort()

    print(f"input records        : {N:,}")
    print(f"input file size      : {keys_mb:.1f} MiB  (gen {t_gen:.1f}s)")
    print(f"memory budget        : {LIMIT / (1 << 20):.0f} MiB")
    print(f"ingest+spill time    : {stats['add_seconds']:.1f} s")
    print(f"merge+emit time      : {stats['merge_seconds']:.1f} s")
    print(f"total operator time  : {stats['add_seconds'] + stats['merge_seconds']:.1f} s")
    print(f"spill runs           : {stats['spills']}")
    print(f"spill bytes written  : {stats['spill_bytes']:,} "
          f"({stats['spill_bytes'] / (1 << 20):.1f} MiB)")
    print(f"merge-pass rewrites  : {stats['merge_bytes']:,}")
    print(f"peak on-disk runs    : {stats['disk_before_merge']:,} "
          f"({stats['disk_before_merge'] / (1 << 20):.1f} MiB)")
    print(f"unique keys emitted  : {stats['unique']:,}")
    print(f"output sha1          : {stats['sha1']}")
    print(f"peak RSS (baseline)  : {rss_peak:.1f} MiB (interpreter alone {rss_base:.1f} MiB)")
    print(f"matches `sort -u`    : {same}  (sort took {sort_seconds:.1f}s)")
    if not same:
        sys.exit("MISMATCH against sort -u")
    # Reclaim benchmark disk.
    for path in (KEYS_FILE, OUT_FILE, EXPECTED_FILE):
        os.remove(path)
    os.rmdir(WORK)


if __name__ == "__main__":
    main()

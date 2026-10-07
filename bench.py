"""Benchmark + data probe for VersionedStore.

Run:  python3 bench.py
Emits:
  - stdout timing report
  - bench_report.json       (timings, memory peaks)
  - verification_data.json  (concurrent consistency check counters + samples)
  - rollback_diff.json      (A/B data comparison around rollback)
"""

import hashlib
import json
import os
import resource
import shutil
import tempfile
import threading
import time
import tracemalloc

from versioned_store import VersionedStore

N_READS = 1_000_000
N_SWITCHES = 1_000
N_KEYS = 128


def payload(old, nid, n=N_KEYS):
    return {f"k{i:04d}": f"v{nid}-d{i}" for i in range(n)}


def snap_hash(s):
    items = sorted(s.view().items())
    return hashlib.sha256(
        (str(s.current_version) + "|" + json.dumps(items)).encode()).hexdigest()[:16]


def timed(fn):
    t0 = time.perf_counter()
    rv = fn()
    return time.perf_counter() - t0, rv


def main():
    report = {}
    s = VersionedStore()
    s.commit(payload)

    # ---- phase 1: 1,000,000 single-key reads (single thread)
    def read_1m():
        acc = 0
        for i in range(N_READS):
            acc += len(s.get(f"k{i % N_KEYS:04d}"))
        return acc
    tracemalloc.start()
    dt, _ = timed(read_1m)
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    report["reads_1m"] = {
        "ops": N_READS, "seconds": round(dt, 4),
        "ops_per_sec": round(N_READS / dt),
        "tracemalloc_peak_bytes": peak,
    }

    # ---- phase 1b: 1,000,000 view() snapshot reads, 4 threads
    stop = threading.Event()
    per = [N_READS // 4] * 4
    t0 = time.perf_counter()
    def reader(n):
        for i in range(n):
            s.get(f"k{i % N_KEYS:04d}")
    ts = [threading.Thread(target=reader, args=(per[j],)) for j in range(4)]
    for t in ts: t.start()
    for t in ts: t.join()
    dt4 = time.perf_counter() - t0
    report["reads_1m_4threads"] = {"ops": N_READS, "seconds": round(dt4, 4),
                                   "ops_per_sec": round(N_READS / dt4)}

    # ---- phase 2: 1,000 switches (commits), in-memory
    tracemalloc.start()
    def do_switches():
        for _ in range(N_SWITCHES):
            s.commit(payload)
    dt_sw, _ = timed(do_switches)
    cur2, peak2 = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    report["switches_1000_inmemory"] = {
        "ops": N_SWITCHES, "seconds": round(dt_sw, 4),
        "switches_per_sec": round(N_SWITCHES / dt_sw),
        "tracemalloc_peak_bytes": peak2,
    }

    # ---- phase 2b: durable switches cost (200 to keep fsync time bounded)
    path = tempfile.mkdtemp(prefix="vs-bench-")
    ds = VersionedStore(path=path)
    ds.commit(payload)
    t0 = time.perf_counter()
    for _ in range(200):
        ds.commit(payload)
    dt_d = time.perf_counter() - t0
    report["switches_durable_200_fsync"] = {
        "ops": 200, "seconds": round(dt_d, 4),
        "switches_per_sec": round(200 / dt_d, 1),
    }
    shutil.rmtree(path, ignore_errors=True)

    # ---- phase 3: concurrent reads during 1000 switches + consistency check
    s2 = VersionedStore()
    s2.commit(payload)
    stop = threading.Event()
    stats = {"views": 0, "keys": 0, "mismatches": 0}
    lock = threading.Lock()
    samples = []

    def checker():
        local_v = local_k = local_bad = 0
        while not stop.is_set():
            v = s2.view()
            tag = v.version_id
            for k, val in v.items():
                if val != f"v{tag}-d{int(k[1:])}":
                    local_bad += 1
                local_k += 1
            local_v += 1
        with lock:
            stats["views"] += local_v
            stats["keys"] += local_k
            stats["mismatches"] += local_bad

    readers = [threading.Thread(target=checker) for _ in range(4)]
    for t in readers: t.start()
    t0 = time.perf_counter()
    for _ in range(N_SWITCHES):
        s2.commit(payload)
    switch_done = time.perf_counter() - t0
    # grab one view mid-flight style snapshot (after writers finished but
    # readers may still be scanning)
    v = s2.view()
    samples.append({"view_version": v.version_id, "first": v.get("k0000"),
                    "last": v.get("k0127"), "keys": len(v)})
    stop.set()
    for t in readers: t.join()
    report["concurrent_1000_switches_4_readers"] = {
        "switch_seconds": round(switch_done, 4),
        "switch_ops_per_sec": round(N_SWITCHES / switch_done),
        "consistent_views": stats["views"],
        "keys_checked": stats["keys"],
        "mixed_reads": stats["mismatches"],
    }

    # ---- phase 4: rollback A/B comparison (对拍数据)
    sr = VersionedStore()
    v1 = sr.commit(lambda old, n: {f"k{i:04d}": f"A-data-{i}" for i in range(N_KEYS)})
    A = sr.view().as_dict(); hA = snap_hash(sr)
    v2 = sr.commit(lambda old, n: {f"k{i:04d}": f"B-data-{i}" for i in range(N_KEYS)})
    B = sr.view().as_dict(); hB = snap_hash(sr)
    sr.rollback(v1)
    A2 = sr.view().as_dict(); hA2 = snap_hash(sr)
    sr.switch_to(v2)
    B2 = sr.view().as_dict(); hB2 = snap_hash(sr)
    rollback_diff = {
        "v1_id": v1, "v2_id": v2, "keys_per_version": N_KEYS,
        "hash_v1_before_rollback": hA,
        "hash_v2_before_rollback": hB,
        "hash_v1_after_rollback": hA2,
        "hash_v2_after_rollforward": hB2,
        "v1_diff_keys_after_rollback": sum(1 for k in A if A[k] != A2.get(k)),
        "v2_diff_keys_after_rollforward": sum(1 for k in B if B[k] != B2.get(k)),
        "v1_unchanged": A == A2,
        "v2_recoverable_after_rollback": B == B2,
        "samples": {
            "v1_before": A["k0000"], "v1_after": A2["k0000"],
            "v2_before": B["k0000"], "v2_after": B2["k0000"],
        },
    }

    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report["process_rss_peak_kb"] = rss_kb
    report["python"] = os.sys.version.split()[0]

    with open("bench_report.json", "w") as fh:
        json.dump(report, fh, indent=2)
    with open("verification_data.json", "w") as fh:
        json.dump({"phase": "concurrent reads during 1000 switches",
                   "stats": report["concurrent_1000_switches_4_readers"],
                   "sample_view": samples}, fh, indent=2)
    with open("rollback_diff.json", "w") as fh:
        json.dump(rollback_diff, fh, indent=2)

    print(json.dumps(report, indent=2))
    print("\nrollback A/B:", json.dumps(rollback_diff, indent=2))


if __name__ == "__main__":
    main()

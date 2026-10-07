"""Benchmark: 1000 nodes, 1,000,000 updates. Run: python3 bench.py"""

import os
import random
import resource
import sys
import time
import tracemalloc

from health_propagation import Cluster, Health, Update

N_NODES = 1000
N_UPDATES = 1_000_000
RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def make_updates(seed=99):
    rng = random.Random(seed)
    updates = []
    seqs = [0] * N_NODES
    for i in range(N_UPDATES):
        nid = rng.randrange(N_NODES)
        seqs[nid] += 1
        r = rng.random()
        if r < 0.02:                      # stale: replay an old seq
            seq = max(0, seqs[nid] - rng.randrange(100, 500))
        elif r < 0.04:                    # duplicate: current seq again
            seq = seqs[nid]
            seqs[nid] -= 1
        else:
            seq = seqs[nid]
        # 5% of nodes flap every update; the rest are mostly UP
        if nid < N_NODES // 20:
            h = Health.UP if seqs[nid] % 2 == 0 else Health.DOWN
        else:
            h = Health.DOWN if rng.random() < 0.01 else Health.UP
        updates.append(Update(f"n{nid}", seq, h, i * 0.001))
    return updates


def run(updates, with_tracemalloc=False):
    c = Cluster()
    if with_tracemalloc:
        tracemalloc.start()
    t0 = time.perf_counter()
    for u in updates:
        c.apply(u)
        if c.accepted % 7 == 0:           # simulate propagation decisions
            c.view(u.node_id, u.ts)
    elapsed = time.perf_counter() - t0
    peak = tracemalloc.get_traced_memory()[1] if with_tracemalloc else 0
    if with_tracemalloc:
        tracemalloc.stop()
    return c, elapsed, peak


def main():
    print(f"building {N_UPDATES:,} updates for {N_NODES} nodes ...")
    updates = make_updates()

    c, elapsed, _ = run(updates)
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    _, elapsed_mem, peak = run(updates, with_tracemalloc=True)

    lines = [
        f"nodes={N_NODES}, updates={N_UPDATES:,}, python={sys.version.split()[0]}",
        f"wall time (clean run):        {elapsed:.2f}s "
        f"({N_UPDATES / elapsed:,.0f} updates/s)",
        f"wall time (tracemalloc run):  {elapsed_mem:.2f}s (instrumented)",
        f"tracemalloc peak (1M run):    {peak / 1024 / 1024:.1f} MiB",
        f"process peak RSS (ru_maxrss): {rss_kb / 1024:.1f} MiB",
        f"merge stats: accepted={c.accepted:,} duplicate={c.duplicates:,} "
        f"stale={c.stale:,} conflict={c.conflicts:,}",
        f"node records retained: {len(c.nodes)}, "
        f"updates/node <= {c.params.history} (bounded)",
    ]
    report = "\n".join(lines)
    print(report)
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "bench.txt"), "w") as f:
        f.write(report + "\n")


if __name__ == "__main__":
    main()

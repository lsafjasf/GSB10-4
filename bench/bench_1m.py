"""Benchmark: 1,000,000 sequential Bayesian observations.

Passes, ordered so RSS high-water marks stay meaningful:
  A: streaming input (likelihoods generated on the fly), timing + RSS
  B: preallocated 1M-row input, timing + RSS
  C: peak Python allocations of the updater alone, measured by tracemalloc

Run:  python3 bench/bench_1m.py
"""

import gc
import resource
import sys
import time
import tracemalloc

sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/src")

from bayeslog import BayesModel  # noqa: E402

N_OBS = 1_000_000
N_HYP = 3


def make_stream(n):
    """Deterministic likelihood stream via a 64-bit LCG."""
    state = 0x9E3779B9
    rows = []
    for _ in range(n):
        row = []
        for _ in range(N_HYP):
            state = (state * 6364136223846793005 + 1442695040888963407) & (
                (1 << 64) - 1
            )
            row.append((state >> 11) / (1 << 53) + 1e-12)
        rows.append(row)
    return rows


def run(rows):
    model = BayesModel([1.0 / N_HYP] * N_HYP)
    for lik in rows:
        model.update(lik)
    return model


def main() -> None:
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # ---- Pass A: streaming input, constant memory ------------------------
    gc.collect()
    state = 0x9E3779B9
    model_s = BayesModel([1.0 / N_HYP] * N_HYP)
    t0 = time.perf_counter()
    for _ in range(N_OBS):
        lik = []
        for _ in range(N_HYP):
            state = (state * 6364136223846793005 + 1442695040888963407) & (
                (1 << 64) - 1
            )
            lik.append((state >> 11) / (1 << 53) + 1e-12)
        model_s.update(lik)
    streaming_time = time.perf_counter() - t0
    rss_stream = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # ---- Pass B: preallocated input --------------------------------------
    t_build0 = time.perf_counter()
    rows = make_stream(N_OBS)
    build_time = time.perf_counter() - t_build0

    gc.collect()
    t1 = time.perf_counter()
    model = run(rows)
    elapsed = time.perf_counter() - t1
    rss_prealloc = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # ---- Pass C: peak Python allocations of the updater ------------------
    del model
    gc.collect()
    tracemalloc.start()
    model = run(rows)
    _, peak_py = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    post = model.posterior()
    post_s = model_s.posterior()

    print(f"observations             : {N_OBS:,}")
    print(f"hypotheses               : {N_HYP}")
    print(f"streaming-run wall time  : {streaming_time:.3f} s "
          f"({streaming_time / N_OBS * 1e6:.2f} us/update incl. LCG)")
    print(f"prealloc-run wall time   : {elapsed:.3f} s "
          f"({elapsed / N_OBS * 1e6:.2f} us/update)")
    print(f"input generation         : {build_time:.3f} s")
    print(f"RSS baseline             : {rss0 / 1024:.1f} MiB")
    print(f"RSS peak, streaming input: {rss_stream / 1024:.1f} MiB "
          "(updater is O(n_hyp))")
    print(f"RSS peak, 1M-row input   : {rss_prealloc / 1024:.1f} MiB")
    print(f"updater peak python alloc: {peak_py / 1024:.1f} KiB (tracemalloc; "
          "records capped at 32)")
    print(f"posterior (prealloc run) : {[f'{p:.6f}' for p in post]}, "
          f"sum={sum(post):.15f}")
    print(f"posterior (stream run)   : {[f'{p:.6f}' for p in post_s]}, "
          f"sum={sum(post_s):.15f}")
    print(f"underflow events         : {model_s.n_underflow_events} total, "
          f"{len(model_s.underflow_records)} first + "
          f"{len(model_s.recent_underflow_records())} recent retained")
    print(f"first event: {model_s.underflow_records[0]}")
    print(f"last event : {model_s.recent_underflow_records()[-1]}")

    assert abs(sum(post) - 1.0) < 1e-9, "posterior no longer normalized!"
    assert abs(sum(post_s) - 1.0) < 1e-9, "posterior no longer normalized!"


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Benchmark: chunking time and checkpoint size for 100k statements.

Writes testdata/benchmark.json and prints a summary table.
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlchunk.chunker import split_and_chunk
from sqlchunk.checkpoint import CheckpointStore
from sqlchunk.executor import ChunkExecutor, RetryPolicy, make_sqlite_executor

N_STATEMENTS = 100_000
CHUNK_SIZES = [64 * 1024, 256 * 1024, 1 << 20, 4 << 20]


def gen_script(n: int) -> bytes:
    rows = []
    for i in range(n):
        rows.append(
            "INSERT OR REPLACE INTO bench VALUES(%d, 'name;%d', %d); -- row %d\n"
            % (i, i, i * 7 % 100000, i)
        )
    return ("CREATE TABLE bench(id INTEGER PRIMARY KEY, name TEXT, v INTEGER);\n"
            + "".join(rows)).encode("utf-8")


def measure(script: bytes, chunk_size: int, workdir: str):
    t0 = time.perf_counter()
    chunks = split_and_chunk(script, chunk_size)
    t1 = time.perf_counter()

    ckpt_path = os.path.join(workdir, "bench.ckpt")
    store = CheckpointStore(ckpt_path, script, chunks, chunk_size)
    store.init_new()
    fresh_size = os.path.getsize(ckpt_path)

    # mark every chunk done to measure the upper-bound checkpoint size
    for c in chunks:
        store.mark_started(c.index)
        store.mark_done(c.index)
    done_size = os.path.getsize(ckpt_path)
    os.unlink(ckpt_path)

    return {
        "chunk_size_bytes": chunk_size,
        "chunk_count": len(chunks),
        "split_and_chunk_seconds": round(t1 - t0, 4),
        "checkpoint_bytes_fresh": fresh_size,
        "checkpoint_bytes_all_done": done_size,
        "checkpoint_bytes_per_chunk": round(done_size / max(1, len(chunks)), 1),
    }


def measure_execution(script: bytes, chunk_size: int, workdir: str):
    db = os.path.join(workdir, "bench.db")
    ckpt = os.path.join(workdir, "exec.ckpt")
    run_chunk, conn = make_sqlite_executor(db)
    t0 = time.perf_counter()
    ex = ChunkExecutor(script, ckpt, run_chunk, chunk_size=chunk_size,
                       policy=RetryPolicy(max_retries=0), sleep=lambda _s: None)
    summary = ex.run()
    elapsed = time.perf_counter() - t0
    conn.close()
    assert summary.status == "done", summary.status
    count = None
    conn = __import__("sqlite3").connect(db)
    count = conn.execute("SELECT COUNT(*) FROM bench").fetchone()[0]
    conn.close()
    return {
        "chunk_size_bytes": chunk_size,
        "execute_seconds": round(elapsed, 3),
        "rows_committed": count,
        "checkpoint_bytes_final": os.path.getsize(ckpt),
    }


def main():
    workdir = tempfile.mkdtemp(prefix="sqlchunk-bench-")
    print("generating %d statements ..." % N_STATEMENTS)
    t0 = time.perf_counter()
    script = gen_script(N_STATEMENTS)
    gen_s = time.perf_counter() - t0
    print("script bytes: %d (%.1f MiB), generated in %.2fs"
          % (len(script), len(script) / (1 << 20), gen_s))

    results = {"statements": N_STATEMENTS, "script_bytes": len(script),
               "generate_seconds": round(gen_s, 3), "runs": []}
    for size in CHUNK_SIZES:
        r = measure(script, size, workdir)
        results["runs"].append(r)
        print("chunk=%8dB  chunks=%6d  split+chunk=%7.3fs  ckpt_fresh=%8dB  "
              "ckpt_done=%8dB  ckpt/chunk=%6.1fB"
              % (r["chunk_size_bytes"], r["chunk_count"],
                 r["split_and_chunk_seconds"], r["checkpoint_bytes_fresh"],
                 r["checkpoint_bytes_all_done"], r["checkpoint_bytes_per_chunk"]))

    # Worst case: chunk_size=1 -> one chunk per statement.
    # Only the fresh checkpoint is written (marking 100k chunks done one by
    # one would rewrite the file 100k times).
    t0 = time.perf_counter()
    tiny_chunks = split_and_chunk(script, 1)
    tiny_split_s = time.perf_counter() - t0
    tiny_ckpt = os.path.join(workdir, "tiny.ckpt")
    tiny_store = CheckpointStore(tiny_ckpt, script, tiny_chunks, 1)
    tiny_store.init_new()
    results["worst_case_one_stmt_per_chunk"] = {
        "chunk_count": len(tiny_chunks),
        "split_and_chunk_seconds": round(tiny_split_s, 3),
        "checkpoint_bytes_fresh": os.path.getsize(tiny_ckpt),
    }
    print("worst case chunk=1B: chunks=%d split+chunk=%.3fs ckpt_fresh=%dB"
          % (len(tiny_chunks), tiny_split_s, os.path.getsize(tiny_ckpt)))
    os.unlink(tiny_ckpt)

    # end-to-end execution at the default 1 MiB chunk size
    results["execution"] = measure_execution(script, 1 << 20, workdir)
    print("execute 100k stmts (1MiB chunks): %.2fs, rows=%d, ckpt=%dB"
          % (results["execution"]["execute_seconds"],
             results["execution"]["rows_committed"],
             results["execution"]["checkpoint_bytes_final"]))

    out = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "testdata", "benchmark.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(results, fh, indent=2)
    print("wrote", out)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""CLI: split an SQL script into chunks and execute it with checkpointing.

Examples
--------
python3 run_chunks.py dump.sql --db app.db \
        --checkpoint dump.ckpt --chunk-size 1048576 --max-retries 3
# after any failure, rerun the exact same command to resume.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlchunk.executor import ChunkExecutor, RetryPolicy, make_sqlite_executor


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("script", help="SQL script file to execute")
    p.add_argument("--db", required=True, help="sqlite3 database file")
    p.add_argument("--checkpoint", required=True, help="checkpoint file path")
    p.add_argument(
        "--chunk-size",
        type=int,
        default=1 << 20,
        help="max chunk size in bytes (default 1048576)",
    )
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument(
        "--fresh",
        action="store_true",
        help="delete an existing checkpoint before starting",
    )
    args = p.parse_args(argv)

    with open(args.script, "rb") as fh:
        script = fh.read()

    if args.fresh and os.path.exists(args.checkpoint):
        os.unlink(args.checkpoint)

    run_chunk, conn = make_sqlite_executor(args.db)
    try:
        executor = ChunkExecutor(
            script=script,
            checkpoint_path=args.checkpoint,
            run_chunk=run_chunk,
            chunk_size=args.chunk_size,
            policy=RetryPolicy(max_retries=args.max_retries),
            sleep=lambda _secs: None,
        )
        summary = executor.run()
    finally:
        conn.close()

    print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
    return 0 if summary.status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())

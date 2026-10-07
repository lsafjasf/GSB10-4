"""演示用命令行：dry-run 执行器把每块追加写入输出文件，可直观验证断点续跑。

  python3 -m sqlchunk.cli script.sql --ckpt ckpt.json --out applied.log
  python3 -m sqlchunk.cli script.sql --ckpt ckpt.json --out applied.log   # 续跑
  python3 -m sqlchunk.cli script.sql --fail-after 3                       # 制造失败
生产环境把 DryRunExecutor 换成真实数据库执行器即可（参考 selftest.SQLiteExecutor）。
"""

from __future__ import annotations

import argparse
import sys

from .runner import Runner, FatalError


class DryRunExecutor:
    def __init__(self, out_path, fail_after=None):
        self.out = open(out_path, "a", encoding="utf-8")
        self.done = 0
        self.fail_after = fail_after

    def __call__(self, chunk_text, chunk_idx):
        if self.fail_after is not None and self.done >= self.fail_after:
            raise FatalError(f"injected failure at chunk {chunk_idx}")
        self.out.write(f"-- chunk {chunk_idx} begin\n{chunk_text}-- chunk {chunk_idx} end\n")
        self.out.flush()
        self.done += 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sqlchunk")
    ap.add_argument("script")
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--out", default="applied.log")
    ap.add_argument("--max-chunk-bytes", type=int, default=1 << 20)
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--fail-after", type=int, default=None,
                    help="成功执行 N 块后注入一次致命失败（演示用）")
    args = ap.parse_args(argv)

    with open(args.script, encoding="utf-8") as f:
        script = f.read()
    runner = Runner(script, args.ckpt, DryRunExecutor(args.out, args.fail_after),
                    max_chunk_bytes=args.max_chunk_bytes, max_retries=args.max_retries)
    rep = runner.run()
    print(f"status={rep.status} executed={rep.executed_chunks} "
          f"failed_chunk={rep.failed_chunk} attempts={rep.attempts}")
    return 0 if rep.status == "done" else 2


if __name__ == "__main__":
    sys.exit(main())

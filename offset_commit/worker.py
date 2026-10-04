"""崩溃测试用处理进程：处理位点 -> 写结果 -> WAL 记录 DONE；
独立推进连续前缀 -> WAL 记录 COMMIT（提交与处理解耦）。

用法:
  python3 worker.py <wal> <output> <n> <commit_every> [kill_after]
"""

import os
import signal
import sys

from offset_commit import OffsetStore


def main() -> None:
    wal_path, out_path, n, commit_every = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    kill_after = int(sys.argv[5]) if len(sys.argv) > 5 else None

    tracker, store, _ = OffsetStore.recover(wal_path, sync=True)

    processed_this_run = 0
    with open(out_path, "a") as out:
        for off in range(n):
            if tracker.is_done(off):
                continue  # 重启后跳过已完成位点（不会跳过任何未处理位点）
            # --- 模拟业务处理：先产出结果（崩溃在这之后/DONE之前只会重放，不会丢） ---
            out.write("%d\n" % off)
            out.flush()
            os.fsync(out.fileno())
            # --- 处理完成落 DONE 日志（fsync），内存位点仅在日志之后更新 ---
            store.log_done(off)
            tracker.mark_done(off)
            processed_this_run += 1

            if processed_this_run % commit_every == 0:
                advanced = tracker.advance()  # 独立提交步骤：只推进连续前缀
                if advanced:
                    store.log_commit(advanced[-1])

            if kill_after is not None and processed_this_run >= kill_after:
                # 模拟 kill -9：无 atexit、无缓冲刷新机会
                os.kill(os.getpid(), signal.SIGKILL)

    advanced = tracker.advance()
    if advanced:
        store.log_commit(advanced[-1])
    store.compact(tracker)
    store.close()


if __name__ == "__main__":
    main()

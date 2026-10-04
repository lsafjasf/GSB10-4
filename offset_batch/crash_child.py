"""崩溃恢复测试的被管子进程：处理 0..n-1，逐位点 WAL，可随时被 SIGKILL。"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from offset_commit import WalCommitter  # noqa: E402


def main() -> int:
    wal_path, processed_log, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
    sleep_s = float(os.environ.get("CHILD_SLEEP_EACH", "0"))

    committer = WalCommitter.recover(wal_path)
    start = committer.table.base

    with open(processed_log, "a", buffering=1) as done_log:
        for offset in range(start, n):
            done_log.write(f"{offset}\n")          # 1) 业务处理
            committer.mark_done(offset)            # 2) 处理成功后落 D + fsync
            if offset % 64 == 0:
                committer.commit()                 # 3) 周期性推进高水位
            if sleep_s:
                time.sleep(sleep_s)
        final_base = committer.commit()
    committer.close()
    print(f"start={start} final_base={final_base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

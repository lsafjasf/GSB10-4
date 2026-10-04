"""百万级压测：python3 bench.py [N]

场景：N 条计划（90% 远期、10% 立即到期），测量
  - 插入总耗时 / 单条均耗 / 该阶段内存峰值(tracemalloc)
  - 堆积 N 条未到期时，空扫描（0 条到期）耗时 —— 验证与未到期数量无关
  - 一次扫描投递全部到期消息的耗时
  - 10 万条取消耗时
  - 关闭后重新打开（WAL 恢复）耗时
  - 进程峰值 RSS（ru_maxrss）与 WAL 文件大小
"""
import os
import resource
import sys
import tempfile
import time
import tracemalloc

from delayq import DelayQueue

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
BASE = 1_700_000_000_000  # 注入的"现在"
PAYLOAD = "x" * 32


TM = os.environ.get("BENCH_NO_TM") != "1"


def phase(name):
    if TM:
        tracemalloc.start()
    t0 = time.perf_counter()
    return name, t0


def done(name, t0, extra=""):
    dt = time.perf_counter() - t0
    if TM:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    else:
        peak = 0
    print(f"{name:<38} {dt:>8.3f} s   peak={peak/2**20:>8.1f} MiB {extra}")
    return dt


def main():
    path = os.path.join(tempfile.mkdtemp(prefix="delayq-bench-"), "queue.log")
    q = DelayQueue(path)

    # 1) 插入 N 条：90% 远期，10% 立即到期
    n_due = N // 10
    name, t0 = phase("insert")
    for i in range(N):
        at = BASE if i < n_due else BASE + 3_600_000 + (i % 3_600_000)
        q.schedule(f"m{i}", at, PAYLOAD)
    done(f"insert {N} schedules", t0, f"({N/max(time.perf_counter()-t0,1e-9):,.0f} ops/s)")

    # 2) 空扫描：堆顶未到期，应 O(1)
    name, t0 = phase("scan-empty")
    got = q.due(BASE - 1)
    done("scan with 0 due (backlog=N)", t0, f"delivered={len(got)}")

    # 3) 扫描投递全部到期（N/10 条）
    name, t0 = phase("scan-drain")
    got = q.due(BASE)
    done(f"scan draining {n_due} due", t0, f"delivered={len(got)}")

    # 4) 取消 10 万条未到期
    n_cancel = min(100_000, N - n_due)
    name, t0 = phase("cancel")
    ok = sum(1 for i in range(n_due, n_due + n_cancel) if q.cancel(f"m{i}") == "ok")
    done(f"cancel {n_cancel} pending", t0, f"ok={ok}")

    # 5) 关闭并恢复
    q.close()
    wal_size = os.path.getsize(path)
    name, t0 = phase("recover")
    q2 = DelayQueue(path)
    done(f"recover from WAL ({N} records)", t0, f"recovered={q2.recovered}")
    q2.close()

    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f"{'process peak RSS (ru_maxrss)':<38} {'':>8}     peak={rss_kb/2**10:>8.1f} MiB")
    print(f"{'WAL file size':<38} {wal_size/2**20:>8.1f} MiB")


if __name__ == "__main__":
    main()

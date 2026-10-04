"""100 万次推送 + 确认基准：耗时与未确认队列内存峰值。

运行：python3 bench.py

内存口径：
- 未确认队列 = 窗口内部的乱序集合 _acked_ahead（未确认消息本身只占一个计数器，
  O(1)；消息体由推送方持有，不在本库内存内）。
- 峰值采样：确认阶段每 1024 次操作取 max(len, sys.getsizeof)。
- 另报 tracemalloc 进程峰值与 ru_maxrss 增量作参照。
"""

import random
import resource
import sys
import time
import tracemalloc

from ack_window import AckWindow, OverflowPolicy

N = 1_000_000
DUP_RATIO = 0.1
CAP = 65536


def fmt_bytes(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%.2f %s" % (n, unit)
        n /= 1024


def scenario_shuffle():
    """场景 A：推 100 万条 -> 乱序确认（含 10% 重复 ACK）。"""
    w = AckWindow(N, policy=OverflowPolicy.DROP_NEW)
    tracemalloc.start()
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    t0 = time.perf_counter()
    for s in range(1, N + 1):
        w.push(s)
    t_push = time.perf_counter() - t0

    order = list(range(1, N + 1))
    random.Random(7).shuffle(order)
    order += random.Random(8).sample(range(1, N + 1), int(N * DUP_RATIO))
    random.Random(9).shuffle(order)

    peak_len = 0
    peak_bytes = 0
    t0 = time.perf_counter()
    for s in order:
        w.ack(s)
        buffered = w.buffered_acks
        if buffered > peak_len:
            peak_len = buffered
            peak_bytes = sys.getsizeof(w._acked_ahead)
    t_ack = time.perf_counter() - t0

    _, tm_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    assert w.base == N and w.unacked == 0
    print("[场景 A] 推送 %d 条后乱序确认（含 %.0f%% 重复 ACK）" % (N, DUP_RATIO * 100))
    print("  push 耗时        : %.3f s (%.0f 次/s)" % (t_push, N / t_push))
    print("  ack  耗时        : %.3f s (%.0f 次/s)" % (t_ack, len(order) / t_ack))
    print("  总耗时           : %.3f s" % (t_push + t_ack))
    print("  重复 ACK 数      : %d（幂等忽略，accepted=%d duplicate=%d）"
          % (w.acks_duplicate, w.acks_accepted, w.acks_duplicate))
    print("  未确认队列峰值   : %d 个乱序序号 / %s（set 实测）" % (peak_len, fmt_bytes(peak_bytes)))
    print("  tracemalloc 峰值 : %s（含测试侧乱序列表）" % fmt_bytes(tm_peak))
    print("  ru_maxrss 增量   : %s" % fmt_bytes((rss1 - rss0) * 1024))
    print()


def scenario_sliding():
    """场景 B：上限 65536 的滑动窗口；窗口满后整批逆序确认，再补头部放行推送。"""
    w = AckWindow(CAP, policy=OverflowPolicy.DROP_NEW)
    tracemalloc.start()
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    BATCH = 10000
    head = 1                     # 下一个待确认的最旧序号
    peak_len = 0
    peak_bytes = 0

    def note_peak():
        nonlocal peak_len, peak_bytes
        buffered = w.buffered_acks
        if buffered > peak_len:
            peak_len, peak_bytes = buffered, sys.getsizeof(w._acked_ahead)

    t0 = time.perf_counter()
    s = 0
    while s < N:
        # 推满一个窗口（未确认数恰好到达上限，不触发超限）
        block_n = min(CAP, N - s)
        for _ in range(block_n):
            s += 1
            w.push(s)
        # 整个窗口分块确认：每块 BATCH 个 ACK 逆序到达（块内最旧的最后到，
        # 先堆在乱序集合），块头到达后整批吸收、水位推进
        end = head + block_n      # 本块最后一条序号（range 右端点）
        for chunk in range(head, end, BATCH):
            stop = min(chunk + BATCH, end)
            for q in range(stop - 1, chunk, -1):
                w.ack(q)
                note_peak()
            w.ack(chunk)
            note_peak()
        head = end
    for q in range(head, N + 1):
        w.ack(q)
    t_all = time.perf_counter() - t0

    _, tm_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    assert w.base == N and w.unacked == 0 and w.dropped == 0
    print("[场景 B] 上限 %d 的滑动窗口，边推边确认共 %d 次推送（每批 %d 条逆序 ACK）"
          % (CAP, N, BATCH))
    print("  总耗时           : %.3f s (%.0f 次/s)" % (t_all, N / t_all))
    print("  未确认数峰值     : %d（受上限约束）" % CAP)
    print("  未确认队列峰值   : %d 个乱序序号 / %s（set 实测）"
          % (peak_len, fmt_bytes(peak_bytes)))
    print("  tracemalloc 峰值 : %s" % fmt_bytes(tm_peak))
    print("  ru_maxrss 增量   : %s" % fmt_bytes((rss1 - rss0) * 1024))
    print()


if __name__ == "__main__":
    print("Python %s, N=%d" % (sys.version.split()[0], N))
    print()
    scenario_shuffle()
    scenario_sliding()

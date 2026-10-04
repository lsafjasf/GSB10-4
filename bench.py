"""压测：100 万次推入 + 100 万次弹出的吞吐与内存峰值；MPMC 100 万守恒。

运行：python3 bench.py
"""

import gc
import platform
import threading
import time
import tracemalloc

from aba_stack import EMPTY, LockedStack, LockFreeStack

N = 1_000_000


def fmt_ops(n, seconds):
    return "%.2f M ops/s" % (n / seconds / 1e6)


def bench_single_thread():
    print("-" * 70)
    print("[1] 单线程吞吐：%d 次 push + %d 次 pop" % (N, N))
    s = LockFreeStack()
    t0 = time.perf_counter()
    for i in range(N):
        s.push(i)
    t1 = time.perf_counter()
    popped = 0
    while s.pop() is not EMPTY:
        popped += 1
    t2 = time.perf_counter()
    assert popped == N, (popped, N)
    print("    push: %.3fs  %s" % (t1 - t0, fmt_ops(N, t1 - t0)))
    print("    pop : %.3fs  %s" % (t2 - t1, fmt_ops(N, t2 - t1)))
    print("    合计: %.3fs  %s（守恒校验 popped=%d == %d）"
          % (t2 - t0, fmt_ops(2 * N, t2 - t0), popped, N))

    lk = LockedStack()
    t0 = time.perf_counter()
    for i in range(N):
        lk.push(i)
    while lk.pop() is not EMPTY:
        pass
    t1 = time.perf_counter()
    print("    对照 LockedStack 合计: %.3fs  %s" % (t1 - t0, fmt_ops(2 * N, t1 - t0)))


def bench_memory():
    print("-" * 70)
    print("[2] 内存峰值（tracemalloc）：%d 次 push 后、%d 次 pop 后" % (N, N))
    gc.collect()
    tracemalloc.start()
    s = LockFreeStack()
    for i in range(N):
        s.push(i)
    cur, peak_push = tracemalloc.get_traced_memory()
    popped = 0
    while s.pop() is not EMPTY:
        popped += 1
    for _ in range(8):
        s.reclaimer.reclaim_now()
    cur2, peak_all = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    st = s.reclaimer.stats()
    assert popped == N
    print("    压满 %d 元素时峰值: %.1f MiB（当前 %.1f MiB）"
          % (N, peak_push / 2**20, cur / 2**20))
    print("    全程峰值: %.1f MiB；弹空并回收后当前: %.2f MiB"
          % (peak_all / 2**20, cur2 / 2**20))
    print("    回收器: retired=%d reclaimed=%d limbo=%d freelist=%d"
          % (st["retired"], st["reclaimed"], st["limbo"], st["freelist"]))
    print("    （freelist 为回收器缓存、供后续 push 复用；无泄漏增长）")


def bench_mpmc():
    print("-" * 70)
    producers, consumers = 4, 4
    per = N // producers
    print("[3] MPMC %d 生产者 x %d 消费者：共 %d 次 push + %d 次 pop"
          % (producers, consumers, N, N))
    s = LockFreeStack()
    popped_lists = [[] for _ in range(consumers)]
    done = threading.Event()

    def producer(pid):
        for i in range(per):
            s.push((pid, i))

    def consumer(cid):
        out = popped_lists[cid]
        while True:
            v = s.pop()
            if v is EMPTY:
                if done.is_set():
                    return
                continue
            out.append(v)

    threads = [threading.Thread(target=producer, args=(p,)) for p in range(producers)]
    threads += [threading.Thread(target=consumer, args=(c,)) for c in range(consumers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads[:producers]:
        t.join()
    done.set()
    for t in threads[producers:]:
        t.join()
    t1 = time.perf_counter()
    all_popped = [v for lst in popped_lists for v in lst]
    distinct = len(set(all_popped))
    assert len(all_popped) == N == distinct, (len(all_popped), distinct)
    assert s.pop() is EMPTY
    print("    耗时: %.3fs  %s" % (t1 - t0, fmt_ops(2 * N, t1 - t0)))
    print("    守恒校验: pushed=%d popped=%d distinct=%d duplicates=0 栈已空"
          % (N, len(all_popped), distinct))
    st = s.reclaimer.stats()
    print("    回收器: retired=%d reclaimed=%d limbo=%d freelist=%d"
          % (st["retired"], st["reclaimed"], st["limbo"], st["freelist"]))


if __name__ == "__main__":
    print("=" * 70)
    print("环境: Python %s @ %s" % (platform.python_version(), platform.machine()))
    bench_single_thread()
    bench_memory()
    bench_mpmc()

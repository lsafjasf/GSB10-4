"""性能对比：加锁（LockedLazy）vs 免锁快路径（Lazy）。

两个场景：
  1. 单线程 1,000,000 次 get()
  2. 8 线程 x 125,000 次（共 1,000,000 次），GIL 下展示快路径优势
另附：32 线程同时首次访问时的工厂创建次数统计。
"""

import threading
import time

from lazy import Lazy, LockedLazy

N = 1_000_000


def bench_single_thread(cls, n=N):
    lazy = cls(lambda: "value")
    lazy.get()  # 先完成初始化
    start = time.perf_counter()
    for _ in range(n):
        lazy.get()
    return time.perf_counter() - start


def bench_multi_thread(cls, threads=8, per_thread=N // 8):
    lazy = cls(lambda: "value")
    lazy.get()

    def worker():
        for _ in range(per_thread):
            lazy.get()

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    start = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return time.perf_counter() - start


def creation_count_demo(threads=32):
    calls = []
    calls_lock = threading.Lock()

    def factory():
        time.sleep(0.02)
        with calls_lock:
            calls.append(1)
        return "singleton"

    lazy = Lazy(factory)
    barrier = threading.Barrier(threads)
    values = []
    values_lock = threading.Lock()

    def worker():
        barrier.wait()
        with values_lock:
            values.append(lazy.get())

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return len(calls), len(values), all(v is values[0] for v in values)


def main():
    print("== 创建次数统计：32 线程同时首次访问 ==")
    created, got, same = creation_count_demo()
    print(f"工厂创建次数: {created}（应为 1）；成功 get() 次数: {got}；"
          f"全部拿到同一对象: {same}\n")

    print(f"== 单线程 {N:,} 次 get() ==")
    locked_t = bench_single_thread(LockedLazy)
    lockfree_t = bench_single_thread(Lazy)
    print(f"加锁 LockedLazy : {locked_t * 1000:9.1f} ms")
    print(f"免锁 Lazy (DCL) : {lockfree_t * 1000:9.1f} ms")
    print(f"提速: {locked_t / lockfree_t:.1f}x "
          f"(每次: 加锁 {locked_t * 1e9 / N:.0f} ns, "
          f"免锁 {lockfree_t * 1e9 / N:.0f} ns)\n")

    print("== 8 线程 x 125,000 次（共 1,000,000 次）==")
    locked_t8 = bench_multi_thread(LockedLazy)
    lockfree_t8 = bench_multi_thread(Lazy)
    print(f"加锁 LockedLazy : {locked_t8 * 1000:9.1f} ms")
    print(f"免锁 Lazy (DCL) : {lockfree_t8 * 1000:9.1f} ms")
    print(f"提速: {locked_t8 / lockfree_t8:.1f}x")


if __name__ == "__main__":
    main()

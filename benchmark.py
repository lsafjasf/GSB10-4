"""100 万次获取/归还的耗时与内存上界测量。

运行：python3 benchmark.py
"""

import gc
import resource
import sys
import time
import tracemalloc

from limiter import ConcurrencyLimiter, Token

N = 1_000_000


def main():
    lim = ConcurrencyLimiter(8)
    sample = lim.acquire()
    token_size = sys.getsizeof(sample)
    lim.release(sample)

    gc.collect()
    gc.disable()  # 只测本逻辑自身的分配，不把 GC 回收混进来
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    tracemalloc.start()

    t0 = time.perf_counter()
    for _ in range(N):
        token = lim.acquire()
        lim.release(token)
    elapsed = time.perf_counter() - t0

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    gc.enable()
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    a = lim.audit()
    print(f"获取/归还次数        : {N:,} 对")
    print(f"总耗时               : {elapsed:.3f} s")
    print(f"吞吐                 : {N / elapsed:,.0f} acquire+release 对/秒")
    print(f"单次往返均耗         : {elapsed / N * 1e6:.3f} us")
    print(f"单个 Token 大小      : {token_size} 字节 (__slots__，固定)")
    print(f"tracemalloc 峰值净增 : {peak / 1024:.1f} KiB（归还后 Token 即回收）")
    print(f"tracemalloc 结束时   : {current / 1024:.1f} KiB")
    print(f"进程 ru_maxrss       : {rss_before} -> {rss_after} KiB "
          f"(增量 {rss_after - rss_before} KiB)")
    print(f"内存上界             : O(capacity + held) 个 Token，"
          f"稳态 held=0 时约 {peak / 1024:.1f} KiB 增量，"
          f"极端持有全部令牌时 <= capacity * {token_size} 字节")
    print(f"守恒核算             : issued={a['issued_total']:,} "
          f"returned={a['returned_total']:,} held={a['held']} "
          f"conserved={a['conserved']}")
    assert a["conserved"] and a["held"] == 0 and a["available"] == 8
    print("OK: 100 万次后计数守恒，available 恢复至 capacity")


if __name__ == "__main__":
    main()

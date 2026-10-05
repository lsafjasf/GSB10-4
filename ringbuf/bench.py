"""吞吐与内存压测：100 万次写 + 100 万次读。

运行：python3 bench.py
"""

import os
import resource
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ringbuf import RingBuffer

N = 1_000_000


def bench_pair(cap: int, chunk: int):
    """N 次 write(chunk) + N 次 read(chunk)，半满稳态。"""
    rb = RingBuffer(cap)
    data = bytes(range(256)) * (chunk // 256 + 1)
    data = data[:chunk]
    rb.write(data * (cap // (2 * chunk)))  # 预热到半满，强制游标持续回绕
    tracemalloc.start()
    t0 = time.perf_counter()
    for _ in range(N):
        rb.write(data)
        rb.read(chunk)
    dt = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    ops = 2 * N  # 一次写 + 一次读
    mb = N * chunk / 1e6
    print(f"[bench] cap={cap:>7} chunk={chunk:>4}B  "
          f"{N:,} 次写+{N:,} 次读  耗时 {dt:.3f}s  "
          f"吞吐 {ops/dt/1e6:6.2f} Mops/s  {mb/dt:8.1f} MB/s  "
          f"tracemalloc峰值 {peak/1024:.1f} KiB")
    return dt


def main():
    print(f"Python {sys.version.split()[0]}  N={N:,}")
    bench_pair(64, 1)        # 单字节读写（最不利情形，游标每次都回绕）
    bench_pair(4096, 64)     # 小块
    bench_pair(65536, 4096)  # 大块
    # 内存占用：对象本身 + 底层存储
    rb = RingBuffer(65536)
    print(f"[mem] RingBuffer(65536) 对象 __sizeof__={rb.__sizeof__()}B "
          f"+ 底层bytearray={rb._buf.__sizeof__()}B "
          f"(与容量成正比，不随读写次数增长)")
    print(f"[mem] 进程峰值 RSS = {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.1f} MiB")


if __name__ == "__main__":
    main()

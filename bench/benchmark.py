"""10 万次重连的状态重建耗时与内存上界。运行：python3 bench/benchmark.py"""
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reconnect import Client, Server

RECONNECTS = 100_000
KEYS = 256


def deep_size(obj, seen=None):
    """递归统计对象内存（字节）。"""
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return 0
    seen.add(oid)
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        size += sum(deep_size(k, seen) + deep_size(v, seen) for k, v in obj.items())
    elif isinstance(obj, (list, tuple, set, frozenset)):
        size += sum(deep_size(i, seen) for i in obj)
    elif hasattr(obj, "__dict__"):
        size += deep_size(vars(obj), seen)
    return size


def main():
    server = Server()
    for i in range(10_000):                    # 预热：1 万条历史事件
        server.publish("inc", f"k{i % KEYS}", 1)

    client = Client()
    client.sync(server)                        # 首次全量

    # ---- 计时：10 万次重连（tracemalloc 关闭，避免测量干扰） ----
    from reconnect import Resume
    resume_count = 0
    t0 = time.perf_counter()
    for i in range(RECONNECTS):
        server.publish("inc", f"k{i % KEYS}", 1)   # 每次断连期间来 1 条新事件
        if i and i % 1000 == 0:
            server.compact(keep_last=500)          # 周期性截断: 服务端内存有硬上界
        decision = client.sync(server)             # 重连 -> 续传 1 条
        resume_count += isinstance(decision, Resume)
    elapsed = time.perf_counter() - t0
    assert client.state == server.state, "10 万次重连后状态不一致!"
    assert resume_count == RECONNECTS

    # ---- 内存：另起 1 万次重连测分配峰值 + 对象深大小 ----
    tracemalloc.start()
    for i in range(10_000):
        server.publish("inc", f"k{i % KEYS}", 1)
        client.sync(server)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    client_bytes = deep_size(client)
    print(f"reconnects          : {RECONNECTS:,}")
    print(f"total time          : {elapsed:.3f} s")
    print(f"avg per reconnect   : {elapsed / RECONNECTS * 1e6:.2f} us")
    print(f"throughput          : {RECONNECTS / elapsed:,.0f} reconnects/s")
    print(f"final state keys    : {len(client.state)}")
    print(f"client object bytes : {client_bytes:,} B "
          f"(state {deep_size(client.state):,} B + checkpoint {deep_size(client.checkpoint):,} B)")
    print(f"tracemalloc peak    : {peak:,} B (10k-reconnect 采样窗口)")
    print(f"state consistent    : {client.state == server.state}")
    print()
    print("memory bound: client = O(K), K = 不同键数(本场景 256), 与重连次数无关;")
    print("              server = O(R), R = 保留窗口内事件数, compact() 后有硬上界。")


if __name__ == "__main__":
    main()

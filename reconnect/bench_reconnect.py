# -*- coding: utf-8 -*-
"""10 万次重连压测：状态重建耗时 + 状态对象内存上界（仅标准库）。

运行：python3 bench_reconnect.py [rounds]
"""
import gc
import resource
import sys
import time
import tracemalloc

from reconnect_lib import Client, Server


def deep_sizeof(obj, seen=None):
    """递归求对象图内存（近似上界，bytes）。"""
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return 0
    seen.add(oid)
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        size += sum(deep_sizeof(k, seen) + deep_sizeof(v, seen)
                    for k, v in obj.items())
    elif isinstance(obj, (list, tuple, set, frozenset)):
        size += sum(deep_sizeof(x, seen) for x in obj)
    return size


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    events_per_round = 3
    key_space = 10_000

    server = Server(retention=2_000_000)   # 回放窗口足够大，保证全程续传
    client = Client("bench")

    # 预热：首次全量
    for i in range(1000):
        server.publish("set", "k%05d" % (i % key_space), i)
    client.sync(server)
    assert client.stats["full"] == 1

    gc.collect()
    tracemalloc.start()
    seq = 1000
    t_pub = 0.0
    t_rebuild = 0.0
    for _ in range(rounds):
        t0 = time.perf_counter()
        for _ in range(events_per_round):
            seq += 1
            server.publish("set", "k%05d" % (seq % key_space), seq)
        t1 = time.perf_counter()
        path = client.sync_once(server)      # 每轮一次断线重连
        t2 = time.perf_counter()
        t_pub += t1 - t0
        t_rebuild += t2 - t1
        assert path == "resume", path
        assert client.offset == server.seq
    elapsed = t_pub + t_rebuild
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # 一致性终检：续传 10 万轮后的状态 == 独立全量重算
    assert client.state == server.state.data
    assert client.store.digest == client.store.recompute_digest()
    assert client.store.digest == server.state.digest

    state_bytes = deep_sizeof(client.state)
    store_bytes = deep_sizeof(client.store.data) + sys.getsizeof(client.store)
    client_total = (deep_sizeof(client.store.data)
                    + sys.getsizeof(client.store)
                    + sys.getsizeof(client))
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    print("=== 100k 重连压测（续传路径） ===")
    print("重连次数            : %d" % rounds)
    print("每轮新增事件        : %d（共 %d 条）" % (events_per_round, rounds * events_per_round))
    print("状态键数            : %d" % len(client.state))
    print("总耗时              : %.3f s（其中状态重建 %.3f s，事件生产 %.3f s）"
          % (elapsed, t_rebuild, t_pub))
    print("单次重连重建均耗时  : %.2f us（判定+续传 %d 条增量事件）"
          % (t_rebuild / rounds * 1e6, events_per_round))
    print("重建吞吐            : %.0f reconnects/s" % (rounds / t_rebuild))
    print("--- 内存 ---")
    print("状态对象(dict)      : %d bytes (%.1f KiB, %d keys)"
          % (state_bytes, state_bytes / 1024, len(client.state)))
    print("StateStore 合计     : %d bytes (%.1f KiB)" % (store_bytes, store_bytes / 1024))
    print("Client 整体         : %d bytes (%.1f KiB)" % (client_total, client_total / 1024))
    print("tracemalloc 峰值    : %d bytes (%.1f MiB)" % (peak, peak / 1048576))
    print("进程峰值 RSS        : %.1f MiB" % (rss_kb / 1024))
    print("--- 内存上界结论 ---")
    print("客户端状态内存 = O(不同 key 数)，与重连次数无关；")
    print("本场景上界 = %d keys -> %.1f KiB（StateStore 含摘要 O(1) 附加）。"
          % (len(client.state), store_bytes / 1024))
    print("服务端回放窗口内存 = O(retention)，本场景 retention=%d。" % server.retention)


if __name__ == "__main__":
    main()

"""10 万连接基准：每秒判定耗时 + 内存上界。

运行：python3 bench_keepalive.py
注意：判定时间全部由测试注入（advance 的参数）；perf_counter 只用于测框架耗时，
不参与任何判定逻辑。
"""

import sys
import time
import tracemalloc

from keepalive import KeepAliveMonitor

N = 100_000
T, I, C = 60.0, 10.0, 3      # 空闲 60s、间隔 10s、3 次探测 => 90s 断阈值
TICKS = 300                  # 稳态模拟 300 秒
ACTIVE_PER_TICK = N // 20    # 每秒 5% 连接有数据往来


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]


def main():
    tracemalloc.start()

    t0 = time.perf_counter()
    m = KeepAliveMonitor(T, I, C)
    for i in range(N):
        m.add(i, t=0.0)
    build_s = time.perf_counter() - t0

    cur, peak = tracemalloc.get_traced_memory()
    print(f"建连 {N:>7,} 个: {build_s*1000:8.1f} ms, "
          f"内存 {cur/1e6:8.1f} MB ({cur/N:.1f} 字节/连接), 峰值 {peak/1e6:.1f} MB")

    # ---- 稳态：有数据往来，每秒推进 1 秒 ----
    tick_ms = []
    events_total = 0
    for s in range(1, TICKS + 1):
        for j in range(ACTIVE_PER_TICK):
            cid = (s * 1_000_003 + j * 7919) % N  # 确定性散布
            if (cid ^ s) & 1:
                m.send_data(cid, 64, float(s))
            else:
                m.recv_data(cid, 64, float(s))
        t1 = time.perf_counter()
        events_total += len(m.advance(float(s)))
        tick_ms.append((time.perf_counter() - t1) * 1000)

    print("\n[稳态] 每秒判定耗时（含 5% 连接的事件重置）:")
    print(f"  平均 {sum(tick_ms)/len(tick_ms):7.3f} ms/秒, "
          f"p50 {pct(tick_ms,.50):7.3f}, p99 {pct(tick_ms,.99):7.3f}, "
          f"最大 {max(tick_ms):7.3f} ms/秒")
    total_conn_ticks = N * TICKS
    print(f"  吞吐 {total_conn_ticks / (sum(tick_ms)/1000):,.0f} 连接·秒/CPU秒, "
          f"稳态事件 {events_total} 个（应为 0：活动始终早于 T）")

    # ---- 半开洪峰：全部静默，探测/判死集中在同一秒到期 ----
    m2 = KeepAliveMonitor(T, I, C)
    for i in range(N):
        m2.add(i, t=0.0)
    tracemalloc.reset_peak()

    waves = []
    for label, when in [("探测第1波", 60.0), ("探测第2波", 70.0),
                        ("探测第3波", 80.0), ("判死", 90.0)]:
        t1 = time.perf_counter()
        ev = m2.advance(when)
        waves.append((label, when, (time.perf_counter() - t1) * 1000, len(ev)))

    _, peak2 = tracemalloc.get_traced_memory()
    print("\n[半开洪峰] 10 万连接同秒到期，每秒判定耗时:")
    for label, when, ms, n in waves:
        print(f"  t={when:5.0f}s {label}: {ms:8.1f} ms, 事件 {n:>7,} 个")
    print(f"  运行期内存峰值 {peak2/1e6:.1f} MB；判定后存活 {m2.alive_count()}（应为 0）")

    # ---- 空跑：无到期事件 ----
    t1 = time.perf_counter()
    for _ in range(1000):
        m2.advance(90.0 + 0.001)
    idle_ms = (time.perf_counter() - t1)
    print(f"\n[空转] 无到期时单次 advance {idle_ms/1000*1000:.3f} ms（1000 次合计 {idle_ms*1000:.2f} ms）")


if __name__ == "__main__":
    sys.exit(main())

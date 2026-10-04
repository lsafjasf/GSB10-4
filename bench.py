"""10 万连接基准：每秒判定耗时与内存上界。纯标准库，时间由参数注入。

运行: python3 bench.py [连接数] [模拟秒数]
"""

import gc
import statistics
import sys
import time

from keepalive import Connection, Event, KeepaliveEngine

IDLE = 30.0
IVAL = 5.0
MAX_PROBES = 3


def measure(n_conns: int, seconds: int) -> dict:
    eng = KeepaliveEngine(IDLE, IVAL, MAX_PROBES)

    # ---- 内存：先取空基线，再建连接，差值即连接状态总占用 ----
    gc.collect()
    baseline = _rss_kb()
    conns = [Connection(0.0) for _ in range(n_conns)]
    gc.collect()
    rss_delta_kb = _rss_kb() - baseline
    per_conn_bytes_obj = sys.getsizeof(Connection(0.0))
    # 列表持有指针 + 对象本体，给一个宽松上界
    list_ptr_bytes = sys.getsizeof(conns)
    bound_per_conn = per_conn_bytes_obj + list_ptr_bytes // n_conns + 8
    bound_total_mb = bound_per_conn * n_conns / (1024 * 1024)

    # 工作负载（确定性，无随机）：
    #  - 70% 活跃连接：每 10s 有入站数据，永不进入探测
    #  - 20% 半开连接：只有 TX，无入站，约 t=45 起陆续 DEAD
    #  - 10% 纯空闲：无任何事件，t=45 全部 DEAD
    n_active = n_conns * 7 // 10
    n_half = n_conns - n_active
    half_tx_end = n_active + n_half // 2  # 前一半半开做 TX，后一半纯空闲

    tick_ms = []
    dead_count = 0
    for sec in range(1, seconds + 1):
        now = float(sec)
        if sec % 10 == 0:
            for i in range(0, n_active):
                eng.on_event(conns[i], Event.RX_DATA, now)
        for i in range(n_active, half_tx_end):
            eng.on_event(conns[i], Event.TX_DATA, now)

        t0 = time.perf_counter()
        eng.tick_many(conns, now)
        tick_ms.append((time.perf_counter() - t0) * 1000.0)

    dead_count = sum(1 for c in conns if c.state.name == "DEAD")

    return {
        "n_conns": n_conns,
        "seconds": seconds,
        "rss_delta_kb": rss_delta_kb,
        "per_conn_obj_bytes": per_conn_bytes_obj,
        "bound_per_conn_bytes": bound_per_conn,
        "bound_total_mb": bound_total_mb,
        "tick_mean_ms": statistics.mean(tick_ms),
        "tick_p50_ms": statistics.median(tick_ms),
        "tick_max_ms": max(tick_ms),
        "throughput_per_s_mean": n_conns / (statistics.mean(tick_ms) / 1000.0),
        "dead_after_run": dead_count,
    }


def _rss_kb() -> int:
    """Linux /proc/self/status VmRSS；非 Linux 回退到 0。"""
    try:
        with open("/proc/self/status", "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    r = measure(n, seconds)
    print(f"连接数:               {r['n_conns']:,}")
    print(f"模拟时长:             {r['seconds']} s")
    print(f"单连接对象:           {r['per_conn_obj_bytes']} B (__slots__)")
    print(f"单连接内存上界:       {r['bound_per_conn_bytes']} B "
          f"(对象 + 列表指针 + 8B 余量)")
    print(f"连接状态总内存上界:   {r['bound_total_mb']:.2f} MB")
    print(f"实测 RSS 增量:        {r['rss_delta_kb'] / 1024:.2f} MB")
    print(f"每秒 tick 耗时 mean:  {r['tick_mean_ms']:.3f} ms")
    print(f"每秒 tick 耗时 p50:   {r['tick_p50_ms']:.3f} ms")
    print(f"每秒 tick 耗时 max:   {r['tick_max_ms']:.3f} ms")
    print(f"判定吞吐(mean):       {r['throughput_per_s_mean']:,.0f} 连接/秒")
    print(f"结束时 DEAD 数:       {r['dead_after_run']:,}")


if __name__ == "__main__":
    main()

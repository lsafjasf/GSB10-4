"""10 万次重连基准：调度耗时 + 状态对象内存峰值。

用法: python3 tools/benchmark.py
场景：连续超时 -> 退避 -> 到期重连，循环 100000 次（max_failures 放宽）。
"""

import gc
import json
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from longpoll import ReconnectPolicy, Reconnector, SeedJitter, VirtualClock  # noqa: E402

N = 100_000
SEED = 20240601


def run_once():
    policy = ReconnectPolicy(max_failures=N + 1)
    clock = VirtualClock(1000.0)
    r = Reconnector(policy, clock, SeedJitter(SEED))
    r.start()
    for _ in range(N):
        clock.advance(policy.request_timeout)
        r.on_timeout()
        clock.advance(r.reconnect_at - clock.now())
        r.on_tick()
    return r


def main():
    # 1) 调度耗时（tracemalloc 关闭，避免干扰计时）
    gc.collect()
    t0 = time.perf_counter()
    r = run_once()
    elapsed = time.perf_counter() - t0

    # 2) 内存峰值（tracemalloc 全程跟踪）
    gc.collect()
    tracemalloc.start()
    run_once()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # 3) 单个状态对象自身大小
    import sys as _sys
    obj_size = _sys.getsizeof(r)

    result = {
        "reconnects": N,
        "seed": SEED,
        "schedule_seconds": round(elapsed, 4),
        "per_reconnect_us": round(elapsed / N * 1e6, 3),
        "tracemalloc_peak_bytes": peak,
        "tracemalloc_peak_kib": round(peak / 1024, 1),
        "state_object_sizeof_bytes": obj_size,
        "python": _sys.version.split()[0],
    }
    out = ROOT / "data" / "benchmark.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

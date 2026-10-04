"""10 万次重连的调度耗时与状态对象内存峰值（仅标准库）。

虚拟时钟驱动，不做任何真实 sleep。内存用 tracemalloc 实测堆分配：
  - 单状态机连续跑 10 万次：验证状态为 O(1)，峰值不随次数增长；
  - 10 万个独立状态机并存：给出典型“每连接一个重连器”的总占用，
    并区分“仅状态对象”与“含注入的时钟/随机源”两种口径。
"""

import gc
import random
import sys
import time
import tracemalloc

sys.path.insert(0, ".")

from longpoll.reconnect import BackoffPolicy, FailReason, Reconnector, VirtualClock  # noqa: E402

N = 100_000
POLICY = BackoffPolicy()

# 状态对象自身（__slots__ 实例）及其可变记账字段，不含注入的共享依赖
_OWN_SLOTS = ("failures", "_mono_t", "_last_raw", "_deadline",
              "next_due", "last_delay", "last_reason", "state")


def own_bytes(rc) -> int:
    import types
    total = sys.getsizeof(rc)
    for name in _OWN_SLOTS:
        v = getattr(rc, name)
        if isinstance(v, (int, float, str, bool, bytes)) or v is None \
                or isinstance(v, types.BuiltinFunctionType):
            total += sys.getsizeof(v)
    return total


def main() -> None:
    clk = VirtualClock()
    rc = Reconnector(clk, POLICY, random.Random(20261005).random)
    per_state = own_bytes(rc)

    # ---- 10 万次重连决策：耗时 + 单状态机内存峰值 ----
    tracemalloc.start()
    t0 = time.perf_counter()
    for _ in range(N):
        rc.start_poll()
        clk.advance(POLICY.request_timeout)
        rc.on_failure(FailReason.TIMEOUT)
        clk.advance(rc.last_delay)
        if rc.failures == POLICY.max_attempts - 1:
            rc.reset()
    elapsed = time.perf_counter() - t0
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # ---- 10 万个独立重连器并存 ----
    gc.collect()
    tracemalloc.start()
    many = [Reconnector(VirtualClock(), POLICY, random.Random(i).random)
            for i in range(N)]
    _, peak_many = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    total_own = sum(own_bytes(x) for x in many)
    del many
    gc.collect()

    print(f"重连决策次数:      {N}")
    print(f"调度总耗时:        {elapsed*1000:.1f} ms")
    print(f"单次决策平均:      {elapsed/N*1e6:.3f} 微秒/次")
    print(f"状态对象自身:      {per_state} B/连接 (O(1)，不随重连次数增长)")
    print(f"单状态机跑10万次:  峰值 {peak} B ({peak/1024:.1f} KiB)，"
          f"结束后残留 {current} B")
    print(f"10万连接总状态:    {total_own/1024/1024:.2f} MiB "
          f"({total_own/N:.0f} B/连接)")
    print(f"10万连接含依赖:    {peak_many/1024/1024:.2f} MiB "
          f"({peak_many/N:.0f} B/连接，含独立 clock/rng)")


if __name__ == "__main__":
    main()

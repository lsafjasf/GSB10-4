"""并发限制器自测：路径令牌数 / 重复归还检出 / 守恒核算 / 并发压测 / 百万次基准。

运行：python3 concurrency_limiter/selftest.py
退出码：0 = 全部通过；1 = 存在失败项。

注：本机为共享主机，耗时数据随宿主机负载波动；断言只设宽松 sanity 上限，
真实耗时以打印的实测值为准。
"""

from __future__ import annotations

import resource
import sys
import threading
import time
import tracemalloc

from limiter import (
    ConcurrencyLimiter,
    LeaseCanceled,
    LimiterError,
    TimeoutError,
)

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"    [{mark}] {label}{(' - ' + detail) if detail else ''}")
    if not cond:
        FAILURES.append(label)


def show_counts(tag: str, lim: ConcurrencyLimiter) -> None:
    a = lim.audit()
    print(
        f"    {tag}: available={a['available']} held={a['held']} "
        f"issued={a['issued']} returned={a['returned']} "
        f"duplicate_returns={a['duplicate_returns']}"
    )


def host_load() -> str:
    try:
        with open("/proc/loadavg") as fh:
            return fh.read().split()[:3]
    except OSError:
        return ["?", "?", "?"]


# ---------------------------------------------------------------------------
# 1. 各路径令牌数
# ---------------------------------------------------------------------------
def scenario_paths() -> None:
    print("\n== 1. 各路径令牌数（capacity=2）==")
    lim = ConcurrencyLimiter(2, "paths")
    show_counts("初始", lim)

    # 正常路径
    with lim.lease():
        show_counts("正常路径: 持牌中", lim)
    show_counts("正常路径: 归还后", lim)
    check("正常路径后 available 恢复为 2", lim.available == 2)

    # 异常路径（业务代码抛异常）
    try:
        with lim.lease():
            show_counts("异常路径: 持牌中", lim)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    show_counts("异常路径: 归还后", lim)
    check("异常路径后 available 恢复为 2", lim.available == 2)

    # 取消路径 A：持牌期间任务被取消（模拟 KeyboardInterrupt / CancelledError）
    try:
        with lim.lease():
            show_counts("取消路径A: 持牌中", lim)
            raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    show_counts("取消路径A: 归还后", lim)
    check("持牌取消后 available 恢复为 2", lim.available == 2)

    # 取消路径 B：等待获取期间被取消（cancel_event）
    t1 = lim.acquire()
    t2 = lim.acquire()  # 占满
    show_counts("取消路径B: 占满后", lim)
    cancel = threading.Event()
    cancel.set()
    try:
        lim.acquire(cancel_event=cancel)
        check("等待中取消应抛 LeaseCanceled", False)
    except LeaseCanceled:
        show_counts("取消路径B: 取消后", lim)
        check("等待中取消不发令牌", lim.available == 0 and lim.held == 2)
    lim.release(t1)
    lim.release(t2)
    show_counts("取消路径B: 全部归还后", lim)

    # 超时路径：获取超时
    t1 = lim.acquire()
    t2 = lim.acquire()
    t0 = time.monotonic()
    try:
        lim.acquire(timeout=0.05)
        check("获取超时应抛 TimeoutError", False)
    except TimeoutError:
        elapsed = time.monotonic() - t0
        show_counts("超时路径: 超时后", lim)
        check("超时未发令牌", lim.available == 0 and lim.held == 2)
        check("超时等待时长合理", 0.04 <= elapsed < 2.0, f"{elapsed*1000:.1f}ms")
    lim.release(t1)
    lim.release(t2)

    # 超时路径：lease 上下文超时
    t1 = lim.acquire()
    t2 = lim.acquire()
    try:
        with lim.lease(timeout=0.02):
            check("lease 超时应抛 TimeoutError", False)
    except TimeoutError:
        pass
    show_counts("超时路径(lease): 超时后", lim)
    lim.release(t1)
    lim.release(t2)
    show_counts("全部路径结束", lim)
    check("全部路径结束后 available=2", lim.available == 2)


# ---------------------------------------------------------------------------
# 2. 重复归还检出
# ---------------------------------------------------------------------------
def scenario_double_release() -> None:
    print("\n== 2. 重复归还 / 伪造归还检出（capacity=1）==")
    lim = ConcurrencyLimiter(1, "dup")
    tok = lim.acquire()
    lim.release(tok)
    show_counts("正常归还后", lim)

    for attempt, action in [
        ("同一令牌第二次归还", lambda: lim.release(tok)),
        ("同一令牌第三次归还", lambda: lim.release(tok)),
        ("归还 None", lambda: lim.release(None)),
        ("归还非 Token 对象", lambda: lim.release(object())),
    ]:
        try:
            action()
            check(f"{attempt} 应被拒绝", False)
        except LimiterError as exc:
            check(f"{attempt} 被拒绝", True, str(exc))
        show_counts(f"{attempt} 后", lim)

    other = ConcurrencyLimiter(1, "other")
    foreign = other.acquire()
    try:
        lim.release(foreign)
        check("跨实例归还应被拒绝", False)
    except LimiterError as exc:
        check("跨实例归还被拒绝", True, str(exc))
    show_counts("跨实例归还后", lim)
    other.release(foreign)

    a = lim.audit()
    check("检出计数 = 5", a["duplicate_returns"] == 5, f"got {a['duplicate_returns']}")
    check("伪造/重复归还未扰动计数", a["available"] == 1 and a["issued"] == a["returned"])


# ---------------------------------------------------------------------------
# 3. 守恒核算
# ---------------------------------------------------------------------------
def scenario_conservation() -> None:
    print("\n== 3. 守恒核算（capacity=3，混合路径）==")
    lim = ConcurrencyLimiter(3, "cons")
    ok = err = 0
    for i in range(1000):
        if i % 3 == 0:
            try:
                with lim.lease():
                    err += 1
                    raise ValueError("x")
            except ValueError:
                pass
        else:
            with lim.lease():
                ok += 1
    a = lim.audit()
    print(f"    台账: {a}")
    print(f"    核算: issued({a['issued']}) == returned({a['returned']}) + held({a['held']})"
          f" -> {a['issued'] == a['returned'] + a['held']}")
    print(f"    核算: held({a['held']}) == capacity({a['capacity']}) - available({a['available']})"
          f" -> {a['held'] == a['capacity'] - a['available']}")
    check("发放数 == 归还数 + 持有数", a["issued"] == a["returned"] + a["held"])
    check("持有数 == capacity - available", a["held"] == a["capacity"] - a["available"])
    check("发放总数 == 正常+异常次数", a["issued"] == ok + err == 1000)


# ---------------------------------------------------------------------------
# 4. 高并发压测
# ---------------------------------------------------------------------------
def scenario_stress(threads: int = 32, capacity: int = 16,
                    iterations: int = 5_000) -> None:
    print(f"\n== 4. 并发压测（{threads} 线程 x {iterations} 次, capacity={capacity}）==")
    lim = ConcurrencyLimiter(capacity, "stress")
    counters = {"inflight": 0, "max_inflight": 0, "dup_rejected": 0, "done": 0}
    c_lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def worker() -> None:
        local_dup = 0
        barrier.wait()
        for _ in range(iterations):
            token = lim.acquire()
            with c_lock:
                counters["inflight"] += 1
                if counters["inflight"] > counters["max_inflight"]:
                    counters["max_inflight"] = counters["inflight"]
            lim.release(token)
            with c_lock:
                counters["inflight"] -= 1
            try:
                lim.release(token)  # 故意重复归还，必须被拒绝
            except LimiterError:
                local_dup += 1
        with c_lock:
            counters["dup_rejected"] += local_dup
            counters["done"] += 1

    t0 = time.monotonic()
    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    elapsed = time.monotonic() - t0

    total = threads * iterations
    a = lim.audit()
    print(f"    总操作={total}  耗时={elapsed:.3f}s  ({total/elapsed:,.0f} 次/秒)")
    print(f"    台账: {a}")
    print(f"    峰值并发持有={counters['max_inflight']} (上限 {capacity})")
    check("发放数 == 总操作数", a["issued"] == total)
    check("归还数 == 总操作数", a["returned"] == total)
    check("持有数归零", a["held"] == 0)
    check("available 恢复满额", a["available"] == capacity)
    check("重复归还全部被检出", a["duplicate_returns"] == counters["dup_rejected"] == total)
    check("并发持有从未越限", counters["max_inflight"] <= capacity,
          f"max={counters['max_inflight']}")
    check("所有线程正常结束", counters["done"] == threads)


# ---------------------------------------------------------------------------
# 5. 100 万次基准：耗时 + 内存上界
# ---------------------------------------------------------------------------
def _run_ops(lim: ConcurrencyLimiter, threads: int, total: int) -> float:
    per_thread = total // threads
    barrier = threading.Barrier(threads)

    def worker() -> None:
        barrier.wait()
        for _ in range(per_thread):
            lim.release(lim.acquire())

    t0 = time.monotonic()
    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    return time.monotonic() - t0


def scenario_benchmark(total: int = 1_000_000) -> None:
    print(f"\n== 5. 基准：{total:,} 次获取归还（宿主机负载 {host_load()}）==")
    tracemalloc.start()
    tracemalloc.clear_traces()

    # 单线程基线（不受宿主机调度噪声影响，反映真实单次开销）
    lim1 = ConcurrencyLimiter(256, "bench-1t")
    el1 = _run_ops(lim1, threads=1, total=total)
    a1 = lim1.audit()
    print(f"    单线程: {el1:.3f}s  ({total/el1:,.0f} 次/秒, "
          f"平均 {el1/total*1e6:.2f}us/次)  台账={a1}")

    # 多线程（64 线程, capacity=256，无等待快路径）
    limN = ConcurrencyLimiter(256, "bench-64t")
    elN = _run_ops(limN, threads=64, total=total)
    aN = limN.audit()
    print(f"    64线程: {elN:.3f}s  ({total/elN:,.0f} 次/秒, "
          f"平均 {elN/total*1e6:.2f}us/次)  台账={aN}")

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024  # KiB->B (Linux)
    print(f"    内存: tracemalloc 峰值={peak/1024:.1f} KiB, 当前={current/1024:.1f} KiB, "
          f"进程峰值 RSS={rss/1024/1024:.1f} MiB")

    check("单线程完成 1,000,000 次且守恒",
          a1["issued"] == a1["returned"] == total and a1["held"] == 0)
    check("64线程完成 1,000,000 次且守恒",
          aN["issued"] == aN["returned"] == total and aN["held"] == 0)
    check("耗时上界: 单线程 < 30s", el1 < 30, f"{el1:.3f}s")
    check("耗时上界: 64线程 < 300s（共享主机噪声容忍）", elN < 300, f"{elN:.3f}s")
    check("内存上界: 限制器相关分配峰值 < 5 MiB", peak < 5 * 1024 * 1024,
          f"peak={peak/1024:.1f} KiB")


def main() -> int:
    print("并发限制器自测  Python", sys.version.split()[0])
    scenario_paths()
    scenario_double_release()
    scenario_conservation()
    scenario_stress()
    scenario_benchmark()
    print("\n== 结果 ==")
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} 项未通过: {FAILURES}")
        return 1
    print("PASS: 全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

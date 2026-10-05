"""leakdetect 开销基准：100 万次获取/释放的检测开销与内存上界。

运行: python3 bench_leakdetect.py [N]   (N 默认 1_000_000)

测量内容（计时与内存分开，避免 tracemalloc 干扰计时）:
  - 基线: 同样次数的句柄创建/销毁，不经跟踪器
  - lean 模式: capture_stacks=False（只记获取点标识）
  - full 模式: capture_stacks=True（捕获调用栈，按站点去重存储）
  - 泄漏驻留: 10 万条未释放记录的精确 Python 对象内存(tracemalloc) 与 RSS
"""

import gc
import os
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from leakdetect import LeakTracker

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
N_SITES = 10
N_LIVE = 100_000


def rss_bytes():
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")


class Handle:
    __slots__ = ()


def run_loop(n, tracker):
    """在 N_SITES 个获取点上执行 n 次 acquire+release。"""
    if tracker is None:
        for i in range(n):
            h = Handle()
            del h
        return
    for i in range(n):
        h = tracker.acquire(Handle(), site=f"site_{i % N_SITES}")
        tracker.release(h)


def time_only(n, capture_stacks=None):
    tracker = None if capture_stacks is None else \
        LeakTracker(f"bench_{capture_stacks}", age_threshold=3600,
                    capture_stacks=capture_stacks)
    t0 = time.perf_counter()
    run_loop(n, tracker)
    dt = time.perf_counter() - t0
    if tracker is not None:
        assert tracker.stats()["balanced"]
    return dt


def memory_during_loop(n, capture_stacks):
    tracker = LeakTracker(f"mem_{capture_stacks}", age_threshold=3600,
                          capture_stacks=capture_stacks)
    gc.collect()
    rss0 = rss_bytes()
    tracemalloc.start()
    run_loop(n, tracker)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_delta = rss_bytes() - rss0
    s = tracker.stats()
    assert s["balanced"] and s["live"] == 0, s
    return peak, rss_delta, s


def retention(n_live, capture_stacks):
    tracker = LeakTracker(f"retain_{capture_stacks}", age_threshold=3600,
                          capture_stacks=capture_stacks)
    gc.collect()
    rss0 = rss_bytes()
    tracemalloc.start()
    handles = [tracker.acquire(Handle(), site=f"leak_site_{i % N_SITES}")
               for i in range(n_live)]
    current, _peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    gc.collect()
    rss_delta = rss_bytes() - rss0
    precise_per = current / n_live
    rss_per = rss_delta / n_live
    for h in handles:
        tracker.release(h)
    return precise_per, rss_per


def fmt(dt, n):
    return f"{n/dt/1e6:.2f} M对/s ({dt/n*1e6:.3f} µs/对, 获取+释放)"


def main():
    print(f"=== 基准: {N} 对获取/释放, Python {sys.version.split()[0]}, "
          f"{N_SITES} 个获取点 ===")
    print("计时区间关闭 GC 与 tracemalloc；内存单独测量。")
    print()
    gc.disable()
    try:
        t_base = time_only(N, None)
        t_lean = time_only(N, False)
        t_full = time_only(N, True)
    finally:
        gc.enable()

    print(f"基线(不跟踪):        {fmt(t_base, N)}")
    print(f"lean(仅获取点标识):  {fmt(t_lean, N)}  净开销 "
          f"{(t_lean-t_base)/N*1e6:.3f} µs/对")
    print(f"full(含调用栈):      {fmt(t_full, N)}  净开销 "
          f"{(t_full-t_base)/N*1e6:.3f} µs/对  (栈在同一站点只存一次)")
    print()

    peak_lean, rss_lean, _ = memory_during_loop(N, False)
    peak_full, rss_full, s = memory_during_loop(N, True)
    print(f"100 万对跑完且全部释放后的内存:")
    print(f"  lean: tracemalloc 峰值 {peak_lean/1e6:.2f} MB, 进程 RSS 增量 "
          f"{rss_lean/1e6:.2f} MB")
    print(f"  full: tracemalloc 峰值 {peak_full/1e6:.2f} MB, 进程 RSS 增量 "
          f"{rss_full/1e6:.2f} MB (栈按 {N_SITES} 个站点去重)")
    print()

    pl_lean, rl_lean = retention(N_LIVE, False)
    pl_full, rl_full = retention(N_LIVE, True)
    print(f"未释放记录驻留内存（{N_LIVE} 条泄漏, 含句柄对象 {sys.getsizeof(Handle())} B）:")
    print(f"  lean: {pl_lean:.0f} B/条 (Python对象精确), {rl_lean:.0f} B/条 (RSS, 含分配器碎片)")
    print(f"  full: {pl_full:.0f} B/条 (Python对象精确), {rl_full:.0f} B/条 (RSS)")
    print()
    bound_lean = N_LIVE * pl_lean
    bound_full = N_LIVE * pl_full
    print("内存上界（与总获取次数无关，只取决于未释放句柄数 L 和站点数 S）:")
    print(f"  lean: M <= {pl_lean:.0f}*L + O(S)            ; 10 万泄漏实测 "
          f"{bound_lean/1e6:.1f} MB")
    print(f"  full: M <= {pl_full:.0f}*L + O(S*栈深度)     ; 10 万泄漏实测 "
          f"{bound_full/1e6:.1f} MB")
    print(f"  全部释放后稳态: O(S)，实测 RSS 增量 lean={rss_lean/1e6:.2f}MB "
          f"full={rss_full/1e6:.2f}MB")
    print()
    print(f"核算: {s['balance']} balanced={s['balanced']} "
          f"正常释放={s['normal_releases']}")


if __name__ == "__main__":
    main()

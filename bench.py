"""基准：100 万次 spawn 派生 + 每次读取/写入本地存储。

运行：
    python3 bench.py                 # 默认 1,000,000 次，分块 10,000
    python3 bench.py 1000000 10000   # 可指定总数与每块并发数

指标：
    总耗时 / 每次派生+读+写耗时 / 吞吐（无 tracemalloc 开销，测真实速度）
    峰值 RSS（resource.ru_maxrss，进程级）
    Python 堆增量（tracemalloc，在 10 万次子运行上单独测，避免计时被污染）
"""

import asyncio
import gc
import sys
import time
import tracemalloc

import resource

from tasklocal import LocalStore, bind, get, set, spawn


async def child_work(i: int) -> tuple:
    # 读 3 个继承键 + 写 1 个自己的键（写必须不影响兄弟/父任务）
    a = get("a")
    b = get("b")
    c = get("nested", "missing")
    set("child_id", i)
    return a, b, c is not None or get("d", 0)


async def run_bench(total: int, chunk: int) -> tuple[float, int]:
    root = LocalStore({"a": 1, "b": "hello", "d": 3.14})
    with bind(root):
        # 预热（消除首次 create_task / dict 扩容的冷启动）
        await asyncio.gather(*(spawn(child_work(i)) for i in range(1000)))

        gc.collect()
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KiB
        t0 = time.perf_counter()
        done = 0
        checksum = 0
        while done < total:
            n = min(chunk, total - done)
            results = await asyncio.gather(
                *(spawn(child_work(done + i)) for i in range(n))
            )
            checksum += sum(r[0] for r in results)
            done += n
        elapsed = time.perf_counter() - t0
        rss_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # 父任务必须从未被子任务污染
        assert "child_id" not in root
        assert root.get("a") == 1
    return elapsed, checksum, rss_before, rss_peak


async def measure_python_heap(n: int = 100_000, chunk: int = 1_000) -> tuple[int, int]:
    """用 tracemalloc 测子任务运行期间的 Python 堆峰值（含 store 复制开销）。"""
    root = LocalStore({"a": 1, "b": "hello", "d": 3.14})
    tracemalloc.start()
    with bind(root):
        gc.collect()
        cur0, peak0 = tracemalloc.get_traced_memory()
        done = 0
        while done < n:
            m = min(chunk, n - done)
            await asyncio.gather(*(spawn(child_work(done + i)) for i in range(m)))
            done += m
        cur1, peak1 = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak1 - peak0, cur1 - cur0


def main() -> None:
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    chunk = int(sys.argv[2]) if len(sys.argv) > 2 else 10_000

    print(f"配置：派生+读+写 total={total:,}  chunk(每块并发)={chunk:,}")
    elapsed, checksum, rss_before, rss_peak = asyncio.run(run_bench(total, chunk))
    print(f"总耗时          : {elapsed:8.3f} s")
    print(f"每次派生+读+写  : {elapsed / total * 1e6:8.3f} µs/op")
    print(f"吞吐            : {total / elapsed:12,.0f} ops/s")
    print(f"峰值 RSS        : {rss_peak / 1024:8.1f} MiB "
          f"(运行前最高 {rss_before / 1024:.1f} MiB，每块最多 {chunk:,} 个子任务存活)")
    print(f"校验和          : {checksum} (全部子任务读到继承值 a=1)")

    peak_inc, cur_inc = asyncio.run(measure_python_heap())
    print(f"Python 堆峰值增量: {peak_inc / 1024 / 1024:7.2f} MiB "
          f"(10 万次子运行、每块 1,000 并发，tracemalloc 口径)")
    print(f"运行后残留堆    : {cur_inc / 1024:7.1f} KiB (近似 0 表示子任务存储已全部回收)")


if __name__ == "__main__":
    main()

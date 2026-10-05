"""基准：10 万次多阶段执行的耗时与状态存储内存上界（仅标准库）。

测量两种负载：
  A. 全部成功（每任务 4 阶段 × 每阶段 2 键）
  B. 中间阶段失败 + 幂等回滚（阶段 2 失败，撤销阶段 0/1）
内存上界：tracemalloc 峰值（保留全部 10 万个 TaskResult 的最坏情形），
          以及不保留结果时的稳态峰值。
"""

import gc
import sys
import time
import tracemalloc

from partial_commit import Stage, Store, Task

N = 100_000
N_STAGES = 4
KEYS_PER_STAGE = 2


def make_stages(fail_at=None):
    def make_apply(i):
        def apply(tx):
            tx.write(f"k{i}a", i)
            tx.write(f"k{i}b", i * 2)
            if i == fail_at:
                raise RuntimeError("boom")
        return apply
    return [Stage(f"s{i}", make_apply(i)) for i in range(N_STAGES)]


def run_workload(fail_at, keep_results):
    store = Store()
    results = [] if keep_results else None
    tracemalloc.start()
    t0 = time.perf_counter()
    for _ in range(N):
        task = Task(store, make_stages(fail_at))
        r = task.run()
        if keep_results:
            results.append((r, task))
    elapsed = time.perf_counter() - t0
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return elapsed, peak, results


def main():
    print(f"Python {sys.version.split()[0]}, N={N}, 每任务 {N_STAGES} 阶段 × {KEYS_PER_STAGE} 键")
    print("-" * 72)

    # A. 全部成功，不保留结果（稳态内存）
    gc.collect()
    elapsed, peak, _ = run_workload(None, keep_results=False)
    print(f"A. 全部成功（不保留结果）")
    print(f"   总耗时: {elapsed:.3f}s  ({elapsed / N * 1e6:.2f} us/任务, {N / elapsed:,.0f} 任务/s)")
    print(f"   稳态内存峰值: {peak / 1024:.1f} KiB")

    # B. 中间阶段失败 + 自动回滚，不保留结果
    gc.collect()
    elapsed, peak, _ = run_workload(2, keep_results=False)
    print(f"B. 阶段2失败+回滚（不保留结果）")
    print(f"   总耗时: {elapsed:.3f}s  ({elapsed / N * 1e6:.2f} us/任务, {N / elapsed:,.0f} 任务/s)")
    print(f"   稳态内存峰值: {peak / 1024:.1f} KiB")

    # C. 全部成功，保留全部 10 万个结果（状态存储内存上界）
    gc.collect()
    elapsed, peak, results = run_workload(None, keep_results=True)
    print(f"C. 全部成功（保留全部 {N} 个 TaskResult，最坏情形）")
    print(f"   总耗时: {elapsed:.3f}s")
    print(f"   内存峰值: {peak / 1024 / 1024:.2f} MiB  ->  上界 ≈ {peak / N:.0f} 字节/任务")
    ok = sum(1 for r, _ in results if r.status == "committed")
    print(f"   状态校验: committed={ok}/{N}")

    # D. 失败路径保留结果 + Task（完整撤销日志，状态存储真正上界）
    gc.collect()
    elapsed, peak, results = run_workload(2, keep_results=True)
    print(f"D. 阶段2失败+回滚（保留全部 {N} 个 Task+撤销日志）")
    print(f"   总耗时: {elapsed:.3f}s")
    print(f"   内存峰值: {peak / 1024 / 1024:.2f} MiB  ->  上界 ≈ {peak / N:.0f} 字节/任务")
    ok = sum(1 for r, _ in results if r.status == "rolled_back" and r.rollback.fully_rolled_back)
    print(f"   状态校验: rolled_back&fully={ok}/{N}")


if __name__ == "__main__":
    main()

"""基准：100 万条记录、10 级排序键的耗时与内存峰值。

运行：python3 benchmark.py
结果同时写入 BENCHMARK.md。

记录采用 10 元素元组（含 id），取值函数读列，降低数据本身的内存占用；
排序键的构建/比较开销与 dict 记录完全相同。
"""

import gc
import platform
import random
import resource
import time
import tracemalloc
from pathlib import Path

from stable_sort_pkg import SortKey, stable_sort, multipass_sort

N = 1_000_000
SEED = 20261005

# 列 0 是 id；列 1..10 对应 k1..k10。
KEYS = [
    SortKey(lambda r: r[1]),                                    # k1  int 0..999
    SortKey(lambda r: r[2], descending=True),                   # k2  str 池 50
    SortKey(lambda r: r[3]),                                    # k3  int 0..9
    SortKey(lambda r: r[4]),                                    # k4  int 0..99999
    SortKey(lambda r: r[5], nulls="first"),                     # k5  10% None
    SortKey(lambda r: r[6], descending=True),                   # k6  bool
    SortKey(lambda r: r[7]),                                    # k7  int 0..99
    SortKey(lambda r: r[8]),                                    # k8  str 池 5
    SortKey(lambda r: r[9]),                                    # k9  int 0..2
    SortKey(lambda r: r[10], descending=True, nulls="last"),    # k10 3% str/NaN
]
COL_NAMES = ["id", "k1", "k2", "k3", "k4", "k5",
             "k6", "k7", "k8", "k9", "k10"]


def generate(n=N):
    rng = random.Random(SEED)
    strings = [f"s{i:03d}" for i in range(50)]
    pool5 = ["alpha", "beta", "gamma", "delta", "epsilon"]
    rows = []
    for i in range(n):
        k10 = rng.randrange(0, 20)
        roll = rng.random()
        if roll < 0.02:
            k10 = "mixed-type"          # 与 int 不可比
        elif roll < 0.025:
            k10 = float("nan")          # 不可比（NaN）
        row = (
            i,
            rng.randrange(0, 1000),                 # k1
            strings[rng.randrange(0, 50)],          # k2
            rng.randrange(0, 10),                   # k3
            rng.randrange(0, 100_000),              # k4
            None if rng.random() < 0.10 else rng.randrange(0, 5),  # k5
            rng.random() < 0.5,                     # k6
            rng.randrange(0, 100),                  # k7
            pool5[rng.randrange(0, 5)],             # k8
            rng.randrange(0, 3),                    # k9
            k10,                                    # k10
        )
        rows.append(row)
    return rows


def rss_mb():
    # Linux: ru_maxrss 单位 KB
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def verify_permutation(rows, result):
    assert len(result) == len(rows)
    seen = set()
    for r in result:
        assert r[0] not in seen
        seen.add(r[0])
    assert len(seen) == len(rows)


def run_timed(rows, label, fn, verify=True):
    gc.collect()
    t0 = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - t0
    if verify:
        verify_permutation(rows, result)
    print(f"{label}: {elapsed:.3f}s")
    return result, elapsed


def main():
    print(f"Python {platform.python_version()}  生成 {N:,} 条 × 10 级键 ...")
    t0 = time.perf_counter()
    rows = generate()
    print(f"数据生成: {time.perf_counter()-t0:.1f}s, "
          f"生成后 RSS 峰值 {rss_mb():.0f} MB")

    # 1) 计时（不挂 tracemalloc，避免探针干扰）
    result, elapsed_once = run_timed(rows, "一次成型 stable_sort ", lambda: stable_sort(rows, KEYS))
    # 重复排序一致性：同批再排一次
    again, elapsed_repeat = run_timed(rows, "重复排序(第 2 次)  ", lambda: stable_sort(rows, KEYS))
    assert [r[0] for r in result] == [r[0] for r in again]

    # 2) 对拍：100 万全量逐级稳定排序（算法路径不同，必须逐位一致）
    multi, elapsed_multi = run_timed(rows, "逐级稳定 multipass    ", lambda: multipass_sort(rows, KEYS))
    assert [r[0] for r in result] == [r[0] for r in multi]
    del multi, again
    gc.collect()

    # 3) 内存峰值：tracemalloc（Python 分配层），统计"排序调用"净增量
    tracemalloc.start()
    gc.collect()
    base_cur, base_peak = tracemalloc.get_traced_memory()
    result2 = stable_sort(rows, KEYS)
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del result2
    gc.collect()
    py_peak_net = (peak - base_cur) / (1024 * 1024)

    # 抽几条结果展示平局兜底
    head = [r[0] for r in result[:5]]
    tail = [r[0] for r in result[-5:]]

    md = f"""# 基准数据：100 万条 × 10 级排序键

- 环境: Python {platform.python_version()}, {platform.machine()}, Linux
- 数据: {N:,} 条 11 列元组（id + 10 个键），固定随机种子 {SEED}，可复现
- 键分布: int(0..999) / str(池50,降序) / int(0..9) / int(0..99999) /
  10% 缺失(NULLS FIRST) / bool(降序) / int(0..99) / str(池5) / int(0..2) /
  int(0..19) 混入约 2% 不可比 str 与 0.5% NaN(降序,NULLS LAST)

## 结果

| 项目 | 数值 |
|---|---|
| stable_sort 一次成型耗时 | {elapsed_once:.3f} s |
| 重复排序第 2 次耗时 | {elapsed_repeat:.3f} s |
| multipass 逐级稳定排序耗时（对拍） | {elapsed_multi:.3f} s |
| 排序调用 Python 层内存净峰值(tracemalloc) | {py_peak_net:.0f} MB |
| 进程 RSS 峰值(ru_maxrss，含 100 万输入数据) | {rss_mb():.0f} MB |

- 双实现 100 万全量逐位一致；重复排序逐位一致；结果为输入的排列（无丢失/重复）。
- 排序结果首 5 条 id: {head}；末 5 条 id: {tail}。
- tracemalloc 净峰值只统计 stable_sort 调用期间 Python 层新增分配
  （约等于 键元组 + _Term 比较项 + 装饰列表）；
  RSS 峰值包含全部输入数据，是更贴近实际部署的上限。

> 复现: `python3 benchmark.py`（耗时随机器波动，内存量级稳定）。
"""
    out = Path(__file__).parent / "BENCHMARK.md"
    out.write_text(md, encoding="utf-8")
    print(f"\ntracemalloc 排序净峰值: {py_peak_net:.0f} MB；"
          f"进程 RSS 峰值: {rss_mb():.0f} MB")
    print(f"已写出 {out}")

if __name__ == "__main__":
    main()

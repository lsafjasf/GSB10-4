"""基准：100 万次插入 / 查找 / 删除的耗时与内存峰值。

运行：
    python3 benchmark.py                # 两种实现各跑 1,000,000 次
    python3 benchmark.py --ops 200000   # 小规模试跑

内存口径：
  - tracemalloc 峰值：建表前开启、键列表已生成，因此只统计表结构自身
    （槽数组 / 桶数组 + 桶对象）的 Python 堆分配。
  - ru_maxrss：整个进程的常驻内存峰值（含键对象等），仅作参考。
"""
import argparse
import json
import random
import resource
import time
import tracemalloc

from hashtable import ProbingHashTable, ChainedHashTable


def run(impl, n, seed):
    rng = random.Random(seed)
    keys = random.Random(seed).sample(range(1 << 40), n)
    absent = random.Random(seed + 1).sample(range(1 << 40, 1 << 41), n // 2)
    lookups = [keys[rng.randrange(n)] if rng.random() < 0.8
               else absent[rng.randrange(len(absent))] for _ in range(n)]
    deletes = rng.sample(keys, int(n * 0.7)) + rng.sample(absent, n - int(n * 0.7))
    rng.shuffle(deletes)

    tracemalloc.start()
    table = impl(initial_capacity=1 << 20)
    result = {"impl": impl.__name__, "ops": n}

    t0 = time.perf_counter()
    for k in keys:
        table[k] = k
    result["insert_s"] = time.perf_counter() - t0
    _, peak_after_insert = tracemalloc.get_traced_memory()

    t0 = time.perf_counter()
    s = 0
    for k in lookups:
        s += table.get(k, 0)
    result["lookup_s"] = time.perf_counter() - t0
    result["_sink"] = s

    t0 = time.perf_counter()
    for k in deletes:
        table.pop(k, None)
    result["delete_s"] = time.perf_counter() - t0

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result["struct_mem_peak_mb"] = round(peak / 2**20, 1)
    result["struct_mem_after_insert_mb"] = round(peak_after_insert / 2**20, 1)
    result["process_peak_rss_mb"] = round(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    result["final_len"] = len(table)
    result["final_capacity"] = table.capacity()
    result["final_load"] = round(table.load_factor(), 3)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ops", type=int, default=1_000_000)
    ap.add_argument("--impl", choices=["probing", "chained", "both"], default="both")
    ap.add_argument("--seed", type=int, default=12345)
    args = ap.parse_args()

    impls = {"probing": ProbingHashTable, "chained": ChainedHashTable}
    for name in (("probing", "chained") if args.impl == "both" else (args.impl,)):
        r = run(impls[name], args.ops, args.seed)
        r.pop("_sink", None)
        print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()

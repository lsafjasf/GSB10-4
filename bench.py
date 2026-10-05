"""性能基准：100 万次插入/查找/删除的耗时与内存峰值，查找探长分布。

每个阶段在独立进程中运行（内存峰值互不污染）：
  python3 bench.py [N]                 # 跑全部阶段
  python3 bench.py [N] oaht|chain|dict # 单实现的增查删基准
  python3 bench.py [N] dist            # 接近上界的探长分布
  python3 bench.py [N] delete          # 连续删除（含朴素实现对照）
  python3 bench.py [N] degen           # 哈希函数退化
N 默认 1_000_000。
"""
import gc
import random
import resource
import sys
import time
import tracemalloc

from oaht import OpenAddressingHashTable
from chaining import ChainingHashTable


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def make_keys(n, seed=42):
    rng = random.Random(seed)
    keys = rng.sample(range(1 << 62), n)
    miss = [k ^ (1 << 61) for k in keys]
    return keys, miss


class DictAdapter:
    def __init__(self):
        self.d = {}
    def put(self, k, v):
        self.d[k] = v
    def get(self, k, default=None):
        return self.d.get(k, default)
    def delete(self, k):
        return self.d.pop(k, None) is not None


def phase_crud(name, make, n):
    keys, miss = make_keys(n)
    print(f"== {name}  N={n:,} ==")
    gc.collect()
    tracemalloc.start()
    t = make()
    t0 = time.perf_counter()
    for i, k in enumerate(keys):
        t.put(k, i)
    t1 = time.perf_counter()
    for k in keys:
        t.get(k)
    t2 = time.perf_counter()
    for k in miss:
        t.get(k)
    t3 = time.perf_counter()
    for k in keys:
        t.delete(k)
    t4 = time.perf_counter()
    _, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    def line(label, dt):
        print(f"  {label:<10} {dt:7.2f}s  {dt/n*1e9:8.0f} ns/op")
    line("插入", t1 - t0)
    line("命中查找", t2 - t1)
    line("未命中查找", t3 - t2)
    line("删除", t4 - t3)
    print(f"  合计 {t4-t0:.2f}s | 表 Python 堆峰值 {py_peak/2**20:.1f} MiB "
          f"| 进程 RSS 峰值 {rss_mb():.1f} MiB")


def fill_to_bound(n, **kw):
    """填到再散列临界点（负载因子≈max_load），返回 (表, 键数)。"""
    t = OpenAddressingHashTable(capacity=int(n / 0.7) + 8,
                                collect_stats=True, **kw)
    cap = t.capacity
    bound = int(0.7 * cap)
    keys, _ = make_keys(max(n, bound), seed=7)
    while len(t) < bound:
        k = keys[len(t)]
        t.put(k, len(t))
        if t.capacity != cap:
            raise RuntimeError("unexpected grow")
    return t, keys[:bound]


def show_pct(label, t):
    pct = t.probe_percentiles()
    print(f"  {label:<8} 均值={pct['mean']:.3f}  p50={pct[0.5]}  p90={pct[0.9]}  "
          f"p99={pct[0.99]}  p99.9={pct[0.999]}  max={pct['max']}")


def phase_dist(n):
    print("== 查找探长分布（再散列临界点，负载因子≈0.70，双重哈希）==")
    t, keys = fill_to_bound(n)
    print(f"  size={len(t):,}  cap={t.capacity:,}  负载={len(t)/t.capacity:.4f}")
    t.probe_hist.clear(); t.max_probe = 0
    for k in keys:
        t.get(k)
    show_pct("命中", t)
    t.probe_hist.clear(); t.max_probe = 0
    rng = random.Random(99)
    for _ in range(len(keys)):
        t.get(rng.randrange(1 << 62) ^ (1 << 61))
    show_pct("未命中", t)
    hist = t.probe_hist
    total = sum(hist.values())
    print("  未命中直方图（探长:占比）: " + "  ".join(
        f"{k}:{v/total*100:.2f}%" for k, v in sorted(hist.items())))
    print("  理论值（双重哈希, a=0.70）：命中 1.72，未命中 3.33；"
          "尾部近似几何分布 P(L>k)≈a^k")


def phase_delete(n):
    print("== 连续删除：本实现（墓碑+再散列清理）==")
    keys, _ = make_keys(n)
    t = OpenAddressingHashTable(collect_stats=True)
    for i, k in enumerate(keys):
        t.put(k, i)
    t0 = time.perf_counter()
    cut = n * 9 // 10
    for k in keys[:cut]:
        t.delete(k)
    t1 = time.perf_counter()
    print(f"  删除 {cut:,} 次: {t1-t0:.2f}s  剩余 size={len(t):,}  "
          f"墓碑={t.tombstones:,}  cap={t.capacity:,}  负载={t.load_factor:.3f}")
    live = keys[cut:]
    t.probe_hist.clear(); t.max_probe = 0
    for k in live:
        t.get(k)
    show_pct("删除后命中", t)
    t.probe_hist.clear(); t.max_probe = 0
    rng = random.Random(7)
    for _ in range(len(live)):
        t.get(rng.randrange(1 << 62) ^ (1 << 61))
    show_pct("删除后未命中", t)

    print("== 对照：朴素实现（删除只立墓碑，从不再散列清理）==")
    t2 = OpenAddressingHashTable(collect_stats=True, naive=True)
    keys2, _ = make_keys(370_000, seed=123)
    while t2.capacity < 524288:                  # 先长大
        t2.put(keys2[len(t2)], len(t2))
    bound = int(0.7 * t2.capacity)
    while len(t2) < bound:                      # 填到 0.70
        t2.put(keys2[len(t2)], len(t2))
    live2 = keys2[bound - bound // 10:]         # 保留 10%
    for k in keys2[: bound - bound // 10]:      # 删除 90%，不清理
        t2.delete(k)
    print(f"  size={len(t2):,}  墓碑={t2.tombstones:,}  cap={t2.capacity:,}  "
          f"(size+墓碑)/cap={(len(t2)+t2.tombstones)/t2.capacity:.3f}")
    t2.probe_hist.clear(); t2.max_probe = 0
    for k in live2:
        t2.get(k)
    show_pct("删除后命中", t2)
    t2.probe_hist.clear(); t2.max_probe = 0
    for _ in range(len(live2)):
        t2.get(rng.randrange(1 << 62) ^ (1 << 61))
    show_pct("删除后未命中", t2)


def phase_degen(n):
    print("== 哈希函数退化（hash_fn 恒为 0，所有键同槽）==")
    m = max(1000, n // 200)                     # 全规模会是 O(n^2)，取 5000
    keys, _ = make_keys(m, seed=55)
    t = OpenAddressingHashTable(hash_fn=lambda k: 0, collect_stats=True)
    t0 = time.perf_counter()
    for i, k in enumerate(keys):
        t.put(k, i)
    t1 = time.perf_counter()
    for k in keys:
        t.get(k)
    t2 = time.perf_counter()
    show_pct("命中探长", t)
    print(f"  {m:,} 插入 {t1-t0:.2f}s，{m:,} 查找 {t2-t1:.2f}s；"
          f"cap={t.capacity:,} 负载={t.load_factor:.2f}，结果全部正确")
    print("  恒定哈希下任何探测方式都退化为线性探测且起点恒为槽0，"
          "探长 Θ(n)；负载上界无法挽救，只能靠哈希混合/随机化防御")


def phase_churn(n):
    """插入/删除交替：朴素墓碑实现的典型退化场景。"""
    print("== 插入/删除交替（churn）：墓碑累积对照 ==")
    cap = 32768
    fill = cap // 2
    for label, naive in [("朴素(墓碑不清理)", True), ("本实现(再散列清理)", False)]:
        rng = random.Random(1)
        pool = iter(range(10_000_000))
        t = OpenAddressingHashTable(capacity=cap, naive=naive, collect_stats=True)
        cur = set()
        for _ in range(fill):
            k = next(pool); t.put(k, 1); cur.add(k)
        print(f"  --- {label} ---")
        for r in range(1, 301):
            dead = rng.sample(sorted(cur), 400)
            for k in dead:
                t.delete(k)
            cur.difference_update(dead)
            for _ in range(400):
                k = next(pool); t.put(k, 1); cur.add(k)
            if r % 50 == 0:
                t.probe_hist.clear(); t.max_probe = 0
                for k in range(-5000, 0):
                    t.get(k)
                mean = t.probe_percentiles()["mean"]
                occ = (len(t) + t.tombstones) / t.capacity
                print(f"    round {r:3d}: cap={t.capacity:6d} size={len(t):6d} "
                      f"墓碑={t.tombstones:6d} (size+墓碑)/cap={occ:.3f} "
                      f"未命中探长均值={mean:8.2f}")


PHASES = {
    "oaht": lambda n: phase_crud(
        "开放地址法 oaht", lambda: OpenAddressingHashTable(), n),
    "chain": lambda n: phase_crud(
        "链地址法 chaining", ChainingHashTable, n),
    "dict": lambda n: phase_crud(
        "内置 dict（参照）", DictAdapter, n),
    "dist": phase_dist,
    "delete": phase_delete,
    "degen": phase_degen,
    "churn": phase_churn,
}


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 1_000_000
    want = sys.argv[2:] or list(PHASES)
    print(f"Python {sys.version.split()[0]}  N={n:,}")
    for p in want:
        PHASES[p](n)
        print()


if __name__ == "__main__":
    main()

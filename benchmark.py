#!/usr/bin/env python3
"""benchmark.py -- 10 万会话 / 100 万次选择的耗时与内存上界。

阶段：
  A. 10 万会话冷绑定（首次哈希选择 + 建 pin）
  B. 100 万次选择（90% 已 pin / 10% 冷绑定新会话）
  C. 漂移：摘除 1 个后端（确定性迁移受影响会话）+ 迁移前后全量对拍
  D. 漂移后 10 万次选择，验证归属确定
内存：单独一轮用 tracemalloc 测 Python 峰值，另报进程 RSS 峰值。
"""

import os
import resource
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sticky import MIGRATE, StickyCoordinator, _checksum

REPORTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
N_SESSIONS = 100_000
N_SELECT = 1_000_000
N_BACKENDS = 10
CAPACITY = 15_000

# 确定性 LCG，保证每次运行序列一致
LCG_A, LCG_C, LCG_M = 6364136223846793005, 1442695040888963407, 1 << 64


class Lcg:
    def __init__(self, seed):
        self.x = seed

    def next(self, mod):
        self.x = (self.x * LCG_A + LCG_C) & (LCG_M - 1)
        return self.x % mod


def sid(i):
    return f"sess-{i:06d}"


def run_workload(trace=False):
    coord = StickyCoordinator([f"b{i:02d}" for i in range(N_BACKENDS)],
                              policy=MIGRATE, capacity=CAPACITY, trace=False)
    rng = Lcg(0x5EED)

    # A. 10 万冷绑定
    t0 = time.perf_counter()
    checksums = {}
    for i in range(N_SESSIONS):
        s = sid(i)
        coord.bind(s)
        coord.write_session(s, lambda p, v, i=i: p.update(seq=i, version=v))
        checksums[s] = _checksum(coord.read_state(s))
    t_a = time.perf_counter() - t0

    # B. 100 万次选择（前 90 万为已 pin，后 10 万为新会话冷绑定）
    t0 = time.perf_counter()
    outcomes = {"ok_pinned": 0, "ok_new": 0, "other": 0}
    for k in range(N_SELECT):
        if k < 900_000:
            s = sid(rng.next(N_SESSIONS))
        else:
            s = sid(N_SESSIONS + (k - 900_000))  # 新会话
        sel = coord.bind(s)
        outcomes[sel.outcome] = outcomes.get(sel.outcome, 0) + 1
    t_b = time.perf_counter() - t0

    # C. 漂移：摘除 b03
    victim = sorted(s for s in list(coord.pins.keys())[:N_SESSIONS]
                    if coord.pin_of(s) == "b03")
    t0 = time.perf_counter()
    stats = coord.remove_backend("b03", graceful=True)
    t_c = time.perf_counter() - t0
    all_sessions = sorted(checksums)  # 只对拍了状态的 10 万基线会话
    diff_bad = sum(
        1 for s in all_sessions
        if coord.backends[coord.pin_of(s)].get_state(s).checksum != checksums[s])

    # D. 漂移后 10 万次选择：必须确定且不变
    before = {s: coord.pin_of(s) for s in all_sessions[:N_SESSIONS]}
    t0 = time.perf_counter()
    deterministic = True
    for k in range(100_000):
        s = sid(rng.next(N_SESSIONS))
        if coord.bind(s).backend != before[s]:
            deterministic = False
            break
    t_d = time.perf_counter() - t0

    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return dict(t_a=t_a, t_b=t_b, t_c=t_c, t_d=t_d, outcomes=outcomes,
                stats=stats, diff_bad=diff_bad, deterministic=deterministic,
                rss_mb=rss_kb / 1024.0, loads=coord.loads(),
                n_victim=len(victim), total_sessions=len(all_sessions))


def main():
    os.makedirs(REPORTS, exist_ok=True)

    # 计时轮（不挂 tracemalloc，避免影响耗时）
    r = run_workload(trace=False)

    # 内存轮：tracemalloc 测 A+B+C 全过程 Python 分配峰值
    tracemalloc.start()
    run_workload(trace=True)
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    r["py_peak_mb"] = peak / 1e6
    r["py_per_session_kb"] = peak / r["total_sessions"] / 1e3

    lines = []
    w = lambda s="": lines.append(s) or print(s)
    w("== 性能数据：10 万会话 / 100 万次选择 ==")
    w(f"环境：Python {sys.version.split()[0]}，{N_BACKENDS} 后端，capacity={CAPACITY}，"
      f"160 虚节点/后端")
    w("")
    w("耗时")
    w(f"  A 冷绑定 100,000 会话（含 1 次状态写） : {r['t_a']:.3f} s  "
      f"({100000 / r['t_a']:,.0f} 次/秒)")
    w(f"  B 1,000,000 次选择                    : {r['t_b']:.3f} s  "
      f"({1000000 / r['t_b']:,.0f} 次/秒)  outcomes={r['outcomes']}")
    w(f"    单次选择耗时上界                      : {r['t_b'] / 1_000_000 * 1e6:.2f} us")
    w(f"  C 漂移：摘除 b03，迁移 {r['stats']['migrated']:,} 会话 : "
      f"{r['t_c']:.3f} s  ({r['stats']['migrated'] / r['t_c']:,.0f} 迁移/秒)")
    w(f"  D 漂移后 100,000 次选择               : {r['t_d']:.3f} s  "
      f"({100000 / r['t_d']:,.0f} 次/秒)  确定={r['deterministic']}")
    w("")
    w("内存")
    w(f"  tracemalloc Python 峰值（A+B+C 后）   : {r['py_peak_mb']:.1f} MB")
    w(f"  折合每会话                            : {r['py_per_session_kb']:.2f} KB")
    w(f"  进程 RSS 峰值（ru_maxrss）            : {r['rss_mb']:.0f} MB")
    w("")
    w("正确性")
    w(f"  漂移迁移统计                          : {r['stats']}")
    w(f"  全量对拍不一致数（期望 0）            : {r['diff_bad']}")
    w(f"  漂移后选择确定性                      : {r['deterministic']}")
    w(f"  漂移后各后端负载                      : {r['loads']}")

    # 复杂度上界（理论）
    w("")
    w("理论上界（n=会话数, m=后端数, v=每后端虚节点=160）")
    w("  pin 表内存   : O(n)，条目为小 dataclass（约 ~150 B/会话）")
    w("  哈希环内存   : O(m*v)，10 后端约 1,600 个点，可忽略")
    w("  选择耗时     : O(log(m*v)) ~ O(1)（bisect + 至多 m 次顺时针探测）")
    w("  摘除迁移耗时 : O(k * log(m*v))，k=该后端上会话数")

    with open(os.path.join(REPORTS, "benchmark.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n已写入 reports/benchmark.txt")

    ok = (r["diff_bad"] == 0 and r["deterministic"] and r["stats"]["lost"] == 0)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

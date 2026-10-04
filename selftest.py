"""object_pool 自测与基准：预热依据、分代变化、残留检测、100 万次借还。

运行：python3 selftest.py
"""

from __future__ import annotations

import resource
import statistics
import time
import tracemalloc
import zlib

from object_pool import GenerationalPool

BUF_SIZE = 4096
_ZERO = bytes(BUF_SIZE)


class Connection:
    """模拟昂贵的连接对象：4KB 缓冲区 + 每连接元数据。"""

    def __init__(self) -> None:
        self.buffer = bytearray(BUF_SIZE)
        self.user: str | None = None
        self.log: list[str] = []

    def reset(self) -> None:
        self.buffer[:] = _ZERO
        self.user = None
        self.log.clear()


def fingerprint(c: Connection):
    return (c.user, tuple(c.log), zlib.crc32(c.buffer))


CLEAN_FP = fingerprint(Connection())


def make_pool(**kw) -> GenerationalPool[Connection]:
    kw.setdefault("factory", Connection)
    kw.setdefault("reset", Connection.reset)
    return GenerationalPool(**kw)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def set(self, t: float) -> None:
        self.t = t


def hdr(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# ---------------------------------------------------------------- E0 冷启动
def e0_cold_start() -> dict:
    hdr("E0 冷创建耗时测量 -> 预热数量依据")
    n = 2000
    samples = []
    keep: list[Connection] = []          # 持有不释放，模拟真实冷启动：
    for _ in range(n):                   # 每次分配都触及全新内存页
        t0 = time.perf_counter()
        keep.append(Connection())
        samples.append(time.perf_counter() - t0)
    del keep
    samples.sort()
    mean = statistics.fmean(samples)
    p50 = samples[n // 2]
    p95 = samples[int(n * 0.95)]
    worst = samples[-1]
    print(f"样本数           : {n}")
    print(f"mean={mean*1e6:8.2f}us  p50={p50*1e6:7.2f}us  "
          f"p95={p95*1e6:7.2f}us  max={worst*1e6:8.2f}us")

    burst = 256            # 启动后预期并发借用峰值（业务侧给定）
    budget_s = 0.100       # 启动阶段允许花在预热上的时间预算 100ms
    capacity = 512
    n_prewarm = min(capacity, burst, int(budget_s / mean))
    print(f"\n预热公式: N = min(capacity={capacity}, burst={burst}, "
          f"budget/mean={int(budget_s/mean)}) = {n_prewarm}")
    print(f"不预热的冷启动代价 = burst * mean = {burst * mean * 1e3:.2f}ms")
    print(f"预热本身耗时     = N * mean    = {n_prewarm * mean * 1e3:.2f}ms "
          f"(<= 预算 {budget_s*1e3:.0f}ms)")
    return {"mean": mean, "burst": burst, "n_prewarm": n_prewarm, "capacity": capacity}


# ------------------------------------------------- E1 预热不足 vs 预热充足
def e1_prewarm_effect(burst: int, n_prewarm: int, capacity: int) -> None:
    hdr("E1 预热不足 vs 预热充足（每轮突发 %d 次，共 50 轮）" % burst)
    rounds = 50

    def burst_borrow(pool: GenerationalPool) -> float:
        t0 = time.perf_counter()
        held = [pool.borrow() for _ in range(burst)]
        for c in held:
            c.buffer[0] ^= 0x01          # 借出即真实读写（触发缺页/缓存加载）
        dt = time.perf_counter() - t0
        for c in held:
            pool.give_back(c)
        return dt

    cold_times = []          # 每轮都是全新冷池 -> 每轮 256 次 miss
    for _ in range(rounds):
        cold = make_pool(capacity=capacity, prewarm_count=0)
        cold_times.append(burst_borrow(cold))
        assert cold.stats.misses == burst
    warm = make_pool(capacity=capacity, prewarm_count=n_prewarm)
    warm_times = [burst_borrow(warm) for _ in range(rounds)]
    assert warm.stats.misses == 0
    cold_times.sort(); warm_times.sort()

    def row(name, a):
        print(f"{name}: p50={a[len(a)//2]*1e3:7.3f}ms  "
              f"p95={a[int(len(a)*0.95)]*1e3:7.3f}ms  "
              f"max={a[-1]*1e3:7.3f}ms")
    row("预热 0 个（全 miss） ", cold_times)
    row(f"预热 {n_prewarm} 个（全 hit）", warm_times)
    med = cold_times[len(cold_times)//2] / max(warm_times[len(warm_times)//2], 1e-9)
    tail = cold_times[-1] / max(warm_times[-1], 1e-9)
    print(f"冷启动放大: 中位数 {med:.1f}x, 最差轮 {tail:.1f}x -> 预热削平尾部抖动")


# ------------------------------------------------------- E2 分代随时间变化
def e2_generations() -> None:
    hdr("E2 分代数量随时间变化（FakeClock，young_ttl=10s old_ttl=30s gc=5s）")
    clock = FakeClock()
    pool = make_pool(capacity=200, prewarm_count=0, young_ttl=10,
                     old_ttl=30, gc_interval=5, clock=clock)
    pool.prewarm(100)
    held = [pool.borrow() for _ in range(30)]   # t=0 借出 30 再归还
    for c in held:
        pool.give_back(c)

    print(f"{'t(s)':>5} {'事件':<16} {'young':>6} {'old':>6} "
          f"{'promoted':>9} {'evict_Y':>8} {'evict_O':>8}")
    events = {12: "借还5个"}
    for t in range(0, 51, 5):
        clock.set(float(t))
        ev = events.get(t, "")
        if t == 12:
            pass
        if t > 0:
            pool.force_gc()
        if t == 10:
            h = [pool.borrow() for _ in range(5)]
            for c in h:
                pool.give_back(c)
            ev = "借还5个"
        y, o = pool.generation_counts()
        s = pool.stats
        print(f"{t:>5} {ev:<16} {y:>6} {o:>6} {s.promoted:>9} "
              f"{s.evicted_young:>8} {s.evicted_old:>8}")
    y, o = pool.generation_counts()
    assert y + o <= pool.capacity


# ------------------------------------------- E3 预热过量 + 长期空闲（真实时钟）
def e3_idle_decay() -> None:
    hdr("E3 预热过量 + 长期空闲（真实时钟，young_ttl=0.2s old_ttl=0.6s gc=0.1s）")
    pool = make_pool(capacity=512, prewarm_count=512,
                     young_ttl=0.2, old_ttl=0.6, gc_interval=0.1)
    held = [pool.borrow() for _ in range(8)]     # 实际负载只有 8 并发
    for c in held:
        pool.give_back(c)
    print(f"{'t(s)':>6} {'young':>6} {'old':>6} {'idle':>6} "
          f"{'evict_Y':>8} {'evict_O':>8}")
    t0 = time.monotonic()
    while True:
        time.sleep(0.1)
        pool.force_gc()
        y, o = pool.generation_counts()
        s = pool.stats
        t = time.monotonic() - t0
        print(f"{t:>6.1f} {y:>6} {o:>6} {y+o:>6} "
              f"{s.evicted_young:>8} {s.evicted_old:>8}")
        if y + o == 0:
            break
        assert t < 5.0, "idle pool failed to decay"
    print("结论：长期空闲后池收缩到 0，内存全部归还。")


# ------------------------------------------------------- E4 泄漏 + 超限
def e4_leak_and_overflow() -> None:
    hdr("E4 借出不还（泄漏）与池满超限行为（capacity=64）")
    pool = make_pool(capacity=64, prewarm_count=64)

    leaked = [pool.borrow() for _ in range(50)]   # 借出 50 个，拒不归还
    extra = [pool.borrow() for _ in range(20)]    # 继续借：14 hit + 6 miss
    s = pool.stats
    print(f"借出 70 未还: outstanding={pool.outstanding} "
          f"max_outstanding={s.max_outstanding} "
          f"misses={s.misses} created={s.created} idle={pool.idle_count()}")
    assert pool.idle_count() == 0
    assert pool.outstanding == 70

    for c in extra:                                # 归还后 20 个
        pool.give_back(c)
    print(f"归还 20 个后 : idle={pool.idle_count()} "
          f"dropped_full={pool.stats.dropped_full}")
    for c in leaked:                               # 泄漏方终于归还，池满触发丢弃
        pool.give_back(c)
    s = pool.stats
    print(f"全部归还后   : idle={pool.idle_count()} (<= capacity=64) "
          f"dropped_full={s.dropped_full}")
    print(f"leak_report  : {pool.leak_report()}")
    assert pool.idle_count() <= 64
    assert s.dropped_full == 70 - 64
    print("结论：泄漏只推高 outstanding 指标，空闲池始终 <= capacity，不无界增长。")


# ------------------------------------------------------------- E5 残留检测
def e5_residue() -> None:
    hdr("E5 复用残留检测（20000 次 弄脏->归还->再借->校验）")
    pool = make_pool(capacity=8, prewarm_count=8)
    residues = 0
    for i in range(20000):
        c = pool.borrow()
        c.user = f"user-{i}"                     # 弄脏：写元数据 + 写缓冲区
        c.log.append(f"query-{i}")
        c.buffer[:64] = bytes(range(64))
        pool.give_back(c)
        c2 = pool.borrow()
        if fingerprint(c2) != CLEAN_FP:
            residues += 1
        pool.give_back(c2)
    print(f"带 reset 的池    : 残留检出 {residues} / 20000")
    assert residues == 0

    dirty_pool = GenerationalPool(Connection, reset=None, capacity=8,
                                  prewarm_count=8)   # 对照组：不 reset
    c = dirty_pool.borrow()
    c.user = "alice"
    c.buffer[0] = 0xFF
    dirty_pool.give_back(c)
    leaked_fp = fingerprint(dirty_pool.borrow())
    print(f"不带 reset 的对照: 残留检出 {int(leaked_fp != CLEAN_FP)} / 1 "
          f"(证明检测器有效)")
    assert leaked_fp != CLEAN_FP


# ---------------------------------------------------- E6 100 万次借还基准
def e6_benchmark() -> None:
    hdr("E6 100 万次借还基准（capacity=8, prewarm=8）")
    pool = make_pool(capacity=8, prewarm_count=8)
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    tracemalloc.start()
    t0 = time.perf_counter()
    n = 1_000_000
    for _ in range(n):
        c = pool.borrow()
        c.buffer[0] ^= 0x01                      # 模拟一次真实读写
        pool.give_back(c)
    dt = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    s = pool.stats
    print(f"借还次数     : {n}")
    print(f"命中率       : {s.hit_rate * 100:.4f}%  "
          f"(hits={s.hits} misses={s.misses})")
    print(f"总耗时       : {dt:.3f}s  ({dt / n * 1e9:.0f} ns/op)")
    print(f"tracemalloc 峰值: {peak / 1024:.1f} KiB")
    print(f"进程 RSS 峰值: {rss1 / 1024:.1f} MiB (基准前 {rss0 / 1024:.1f} MiB)")
    print(f"对象创建总数 : {s.created} (复用 {s.hits} 次)")
    assert s.hit_rate > 0.999


def main() -> None:
    info = e0_cold_start()
    e1_prewarm_effect(info["burst"], info["n_prewarm"], info["capacity"])
    e2_generations()
    e3_idle_decay()
    e4_leak_and_overflow()
    e5_residue()
    e6_benchmark()
    hdr("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()

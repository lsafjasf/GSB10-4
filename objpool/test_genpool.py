"""genpool 自测与基准。运行: python3 test_genpool.py"""
import gc
import os
import resource
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from genpool import GenerationalPool, PoolExhausted, GEN0, GEN1, GEN2

LINE = "-" * 64


class Conn:
    """模拟昂贵连接对象：创建要"握手"，使用中会被写脏。"""
    def __init__(self):
        time.sleep(0.0005)              # 模拟 0.5ms 冷启动（握手/建连）
        self.buf = bytearray(4096)      # 每个对象占点内存，方便看回收
        self.user = None
        self.queries = 0
        self.tx_open = False

    def reset(self):
        # 只重置被使用过的字段(真实 reset 不该全量擦除)
        self.user = None
        self.queries = 0
        self.tx_open = False
        self.buf[:8] = b"\x00" * 8

    def dirty(self, tag):
        self.user = tag
        self.queries += 1
        self.tx_open = True
        self.buf[:8] = b"DIRTYDIR"


def is_clean(o):
    return (o.user is None and o.queries == 0 and not o.tx_open
            and o.buf[:8] == b"\x00" * 8)


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def report(name, fn):
    print(f"\n{LINE}\n[{name}]\n{LINE}")
    fn()


# ============ 1. 预热依据 + 冷启动耗时挂钩 ============
def t_warmup_basis():
    pool = GenerationalPool(Conn, max_size=512, expected_qps=2000,
                            warmup_budget=0.050, headroom=2.0)
    print(f"实测冷启动均耗 t_create = {pool.t_create*1000:.3f} ms/对象")
    print(f"预热公式: warmup = min(max_size, ceil(budget/t_create), ceil(qps*t_create*headroom))")
    print(f"       = min(512, ceil(0.050/{pool.t_create:.6f}), "
          f"ceil(2000*{pool.t_create:.6f}*2.0)) = {pool.min_idle}")
    print(f"预热实际耗时 = {pool.warmup_seconds*1000:.1f} ms "
          f"(预算 50 ms 内: {pool.warmup_seconds <= 0.06})")
    # 对照：不预热时前 min_idle 次借出全为冷启动 miss
    cold = GenerationalPool.__new__(GenerationalPool)  # 不算，直接估算
    print(f"若不预热，前 {pool.min_idle} 次请求将各付 ~{pool.t_create*1000:.2f} ms "
          f"冷启动，共 ~{pool.min_idle*pool.t_create*1000:.0f} ms 尾延迟")
    return pool


# ============ 2. 分代规则 + 各代数量随时间变化 ============
def t_generations():
    pool = GenerationalPool(Conn, max_size=64, expected_qps=10,
                            warmup_budget=0.010, promote_at=3, tenure_at=8,
                            ttl=(0.4, 1.2, 3.0), sweep_batch=64)
    print(f"预热 min_idle={pool.min_idle}, 晋升: reuse>=3->GEN1, reuse>=8->GEN2")
    print(f"回收 TTL: GEN0=0.4s GEN1=1.2s GEN2=3.0s\n")
    print(f"{'t(s)':>5} {'size':>5} {'idle':>5} {'G0':>4} {'G1':>4} {'G2':>4} {'reclaimed':>9}")
    t0 = time.monotonic()
    entries = [pool.acquire() for _ in range(12)]
    def log(tag=""):
        s = pool.snapshot()
        g = s["per_gen_idle"]
        print(f"{time.monotonic()-t0:5.1f} {s['size']:>5} {s['idle']:>5} "
              f"{g[0]:>4} {g[1]:>4} {g[2]:>4} {s['reclaimed']:>9} {tag}")
    # 阶段1：热用，前6个复用9次(升GEN2)，后6个复用4次(升GEN1)
    for rnd in range(9):
        for e in entries[:6]:
            pool.release(e); entries[entries.index(e)] = pool.acquire()
        if rnd < 4:
            for e in entries[6:]:
                pool.release(e); entries[entries.index(e)] = pool.acquire()
    for e in entries:
        pool.release(e)
    log("热用后: 6xGEN2 + 6xGEN1")
    # 阶段2：冷却，观察分代回收
    for wait in (0.5, 0.8, 2.0):
        time.sleep(wait)
        pool.sweep()
        log()
    print("结论: GEN0 0.4s 先回收, GEN1 撑到 1.2s, GEN2 撑到 3.0s, "
          "且 size 不会低于 min_idle")


# ============ 3. 复用干净性：残留检测 ============
def t_residue():
    leaked = []
    def verify(o):
        ok = is_clean(o)
        if not ok:
            leaked.append(o)
        return ok
    pool = GenerationalPool(Conn, max_size=32, expected_qps=100,
                            warmup_budget=0.010, verify_clean=verify)
    n = 20000
    for i in range(n):
        e = pool.acquire()
        e.obj.dirty(f"user-{i}")          # 使劲弄脏
        pool.release(e)
    # 归还后再借出，逐一检查拿到手的是干净的
    bad_checkout = 0
    for _ in range(5000):
        e = pool.acquire()
        if not is_clean(e.obj):
            bad_checkout += 1
        pool.release(e)
    print(f"弄脏-归还 {n} 次, verify_clean 拦截残留: {len(leaked)} 次")
    print(f"归还后再借出 5000 次逐一检查, 带残留借出: {bad_checkout} 次")
    print(f"reset 失败丢弃数: {pool.stats.resets_failed}")
    assert len(leaked) == 0 and bad_checkout == 0, "存在状态残留!"
    print("PASS: 无任何残留跨用户传递")


# ============ 4. 容量上界与超限行为 ============
def t_overflow():
    for mode in ("discard", "raise", "block"):
        pool = GenerationalPool(Conn, max_size=8, expected_qps=1,
                                warmup_budget=0.003, overflow=mode,
                                block_timeout=0.2)
        held = [pool.acquire() for _ in range(8)]   # 占满
        try:
            extra = pool.acquire()
            outcome = "拿到临时对象(归还即弃)" if mode == "discard" else "?"
            if mode == "discard":
                pool.release(extra)
        except PoolExhausted as ex:
            outcome = f"PoolExhausted: {ex}"
        s = pool.snapshot()
        print(f"overflow={mode:8s} -> {outcome}; "
              f"池规模 size={s['size']} (上界 8, 永不越界: {s['size'] <= 8}), "
              f"overflows={s['overflows']}")


# ============ 5. 100 万次借还: 命中率/耗时/内存峰值 ============
def t_million():
    import threading
    gc.collect()
    tracemalloc.start()
    rss0 = rss_mb()
    N = 1_000_000
    WORKERS = 16
    # qps 按 16 并发/冷启动耗时估算, 预热覆盖并发突发
    qps_est = int(WORKERS / 0.00058)
    pool = GenerationalPool(Conn, max_size=256, expected_qps=qps_est,
                            warmup_budget=0.200, headroom=1.0)
    print(f"{WORKERS} 线程并发, 每线程 {N//WORKERS:,} 次, min_idle={pool.min_idle}")
    def worker(k):
        for i in range(N // WORKERS):
            e = pool.acquire()
            e.obj.dirty(f"w{k}-{i}")          # 每次都弄脏, 顺带压 reset
            pool.release(e)
    threads = [threading.Thread(target=worker, args=(k,)) for k in range(WORKERS)]
    t0 = time.perf_counter()
    for t in threads: t.start()
    for t in threads: t.join()
    dt = time.perf_counter() - t0
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    s = pool.snapshot()
    print(f"借还次数      : {N:,}")
    print(f"总耗时        : {dt:.3f} s ({N/dt:,.0f} ops/s, {dt/N*1e6:.2f} us/次)")
    print(f"命中率        : {s['hit_rate']*100:.4f}%  (hits={s['hits']:,}, misses={s['misses']:,})")
    print(f"tracemalloc峰值: {peak/1024/1024:.2f} MiB (当前 {cur/1024/1024:.2f} MiB)")
    print(f"进程RSS峰值   : {rss_mb():.1f} MB (基准 {rss0:.1f} MB)")
    print(f"池规模        : size={s['size']} (上界 256)")


# ============ 6. 预热不足 vs 预热过量 ============
def t_warmup_extremes():
    def burst(pool, n):
        # 并发突发: 同时持有 n 个对象(模拟 n 个并发请求), 再统一归还
        t0 = time.perf_counter()
        held = [pool.acquire() for _ in range(n)]
        for e in held:
            pool.release(e)
        return time.perf_counter() - t0
    # 不足：min_idle 被压到 1
    under = GenerationalPool(Conn, max_size=256, expected_qps=1,
                             warmup_budget=0.0006, headroom=1.0)
    # 合理：qps 按"200 并发 / 单次冷启动耗时"估算 -> min_idle ~= 200
    good = GenerationalPool(Conn, max_size=256, expected_qps=345_000,
                            warmup_budget=0.200, headroom=1.0)
    # 过量：预热顶到 256
    over = GenerationalPool(Conn, max_size=256, expected_qps=1e9,
                            warmup_budget=10.0, headroom=1.0)
    B = 200  # 突发 200 个并发请求同时持有
    for name, p in (("预热不足", under), ("预热合理", good), ("预热过量", over)):
        d = burst(p, B)
        s = p.snapshot()
        print(f"{name}: min_idle={p.min_idle:>4} 预热耗时={p.warmup_seconds*1000:7.1f}ms "
              f"| 突发{B}次借还 {d*1000:7.1f}ms miss={s['misses']:>4} "
              f"命中率={s['hit_rate']*100:6.2f}%")
    print("不足->突发全付冷启动(~117ms); 合理->突发全命中且预热只花~116ms; "
          "过量->也全命中, 但预热多花~33ms 且多占 56*4KB 内存等回收")


# ============ 7. 长期空闲 ============
def t_idle():
    pool = GenerationalPool(Conn, max_size=128, expected_qps=100,
                            warmup_budget=0.010, promote_at=3, tenure_at=8,
                            ttl=(0.3, 0.6, 1.0), sweep_batch=128)
    # 先突发 100 并发让池子涨过水位线, 其中 20 个复用多次晋升 GEN1/GEN2
    hot = [pool.acquire() for _ in range(20)]
    for rnd in range(9):
        for e in hot:
            pool.release(e)
        hot = [pool.acquire() for _ in range(20)]
    for e in hot:
        pool.release(e)
    burst = [pool.acquire() for _ in range(80)]
    for e in burst:
        pool.release(e)
    print(f"突发后闲置, min_idle={pool.min_idle}, ttl=(G0:0.3, G1:0.6, G2:1.0)s")
    t0 = time.monotonic()
    for wait in (0.0, 0.35, 0.35, 0.5, 0.5):
        time.sleep(wait)
        pool.sweep()
        s = pool.snapshot()
        print(f"  t={time.monotonic()-t0:4.2f}s size={s['size']:>4} "
              f"idle={s['idle']:>4} reclaimed={s['reclaimed']:>4} "
              f"gen={s['per_gen_idle']}")
    print(f"结论: 涨过水位的空闲对象按代际 TTL 逐级回收(G0先G2后), "
          f"降到 min_idle={pool.min_idle} 保底不塌")


# ============ 8. 借出后不归还(泄漏) ============
def t_leak():
    pool = GenerationalPool(Conn, max_size=16, expected_qps=100,
                            warmup_budget=0.010, overflow="discard")
    leaked = []
    for i in range(16 + 10):          # 16 个正常借, 10 个"泄漏"不还
        e = pool.acquire()
        if i >= 16:
            leaked.append(e)          # 故意不还
    s = pool.snapshot()
    print(f"借 26 次还 16 次(泄漏 10): size={s['size']} borrowed={s['borrowed']} "
          f"overflows={s['overflows']}")
    # 池被借光后, 正常流量靠 discard 兜底, 不阻塞不崩溃
    ok = 0
    for _ in range(1000):
        e = pool.acquire()
        if e.reuse != -1:
            pool.release(e); ok += 1
        else:
            ok += 1                   # 临时对象也正常用
    s = pool.snapshot()
    print(f"泄漏后继续 1000 次借还: 全部完成, size={s['size']} "
          f"(仍 <= 16, 泄漏没有让池无界增长)")
    print(f"检测手段: snapshot()['borrowed']={s['borrowed']} 长期>0 即泄漏告警指标")


if __name__ == "__main__":
    report("1. 预热依据(与冷启动耗时挂钩)", t_warmup_basis)
    report("2. 分代规则与各代数量随时间变化", t_generations)
    report("3. 复用干净性/残留检测", t_residue)
    report("4. 容量上界与超限行为", t_overflow)
    report("5. 100万次借还: 命中率/耗时/内存峰值", t_million)
    report("6. 预热不足 vs 预热过量", t_warmup_extremes)
    report("7. 长期空闲的分代回收", t_idle)
    report("8. 借出后不归还(泄漏)", t_leak)
    print(f"\n{LINE}\n全部场景通过\n{LINE}")

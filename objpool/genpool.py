"""genpool: 分代 + 预热的对象池（仅标准库）。

设计要点
========
预热数量
--------
预热不是拍脑袋：先对工厂函数做 ``calibrate()`` 采样，测出单个对象的
冷启动耗时 ``t_create``（秒，取中位数）。预热数量取：

    warmup = min(max_size,
                 ceil(warmup_budget / t_create),        # 预热时间有界
                 ceil(expected_qps * t_create * headroom))  # 覆盖创建窗口内的突发

含义：一个对象创建期间会到达 ``qps*t_create`` 个请求，预热至少要覆盖
这波请求，否则它们全付冷启动价；再乘 headroom(突发余量)。与预算、
容量三者取最小。

分代与回收
----------
三代，按"被复用次数"晋升，按"空闲时长"分代回收：

- GEN0 年轻代：新建对象，复用 < promote_at 次，空闲超过 ttl[0] 即回收；
- GEN1 中年代：复用 >= promote_at 次，空闲超过 ttl[1] 才回收；
- GEN2 年老代：复用 >= tenure_at 次，空闲超过 ttl[2] 才回收。

回收时机：每次 release 惰性触发（每 ``sweep_every`` 次至多一次，由一把
短锁保护，均摊 O(1)），也可后台线程周期 ``sweep()``。池规模降到
``min_idle``（= 预热量）即停止回收，保证水位不塌。

并发模型（CPython 友好）
------------------------
借/还热路径完全无锁：``collections.deque.popleft/append`` 在 GIL 下
原子。只有"池空要新建 / 判容量 / block 等待 / 分代回收"才走锁。
sweep 期间用 ``_sweeping`` 标志让个别线程短暂走锁等待（sweep 很快）。
hits 计数器在热路径上是尽力计数（GIL 下误差可忽略）；misses、
overflows、reclaimed、size 均在锁内维护，精确。

容量上界与超限
--------------
``max_size`` 是硬上界（池管对象总数 = 空闲 + 借出）。池空且未达上界
-> 新建(miss)；已达上界 -> 按 ``overflow`` 策略：
  - "discard": 新建一个**不受池管理**的对象，归还即弃（默认）；
  - "raise"  : 立即抛 PoolExhausted；
  - "block"  : 阻塞等待，超时抛 PoolExhausted。
池永不无界增长：池管对象 <= max_size。

干净复用
--------
归还时调用 ``reset(obj)`` 或 ``obj.reset()``；重置失败 / verify_clean
不通过的对象直接丢弃、缩池，绝不放回。
"""

from __future__ import annotations

import itertools
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field

GEN0, GEN1, GEN2 = 0, 1, 2


class PoolExhausted(Exception):
    """overflow='raise' 或 block 超时时抛出。"""


@dataclass
class _Entry:
    obj: object
    gen: int = GEN0
    reuse: int = 0
    idle_since: float = 0.0


@dataclass
class Stats:
    hits: int = 0          # 热路径尽力计数
    misses: int = 0        # 精确(锁内): 池空新建
    overflows: int = 0     # 精确: 触及上界次数
    reclaimed: int = 0     # 精确: 分代回收数
    resets_failed: int = 0 # 精确: reset 失败被丢弃数
    borrows: int = 0
    releases: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class GenerationalPool:
    def __init__(
        self,
        factory,
        reset=None,
        max_size: int = 1024,
        expected_qps: float = 1000.0,
        warmup_budget: float = 0.050,
        headroom: float = 2.0,
        overflow: str = "discard",
        block_timeout: float = 5.0,
        promote_at: int = 3,
        tenure_at: int = 16,
        ttl=(2.0, 30.0, 300.0),
        sweep_batch: int = 8,
        sweep_every: int = 256,
        calibrate_samples: int = 64,
        verify_clean=None,
    ):
        assert overflow in ("discard", "block", "raise")
        self.factory = factory
        self.reset = reset
        self.max_size = max_size
        self.overflow = overflow
        self.block_timeout = block_timeout
        self.promote_at = promote_at
        self.tenure_at = tenure_at
        self.ttl = ttl
        self.sweep_batch = sweep_batch
        self.sweep_every = sweep_every
        self.verify_clean = verify_clean

        self._idle: deque[_Entry] = deque()
        self._size = 0
        self._borrowed = 0
        self._sweeping = False
        self._sweep_tick = itertools.count(1)
        self._mu = threading.Lock()
        self._cond = threading.Condition(self._mu)
        self._waiters = 0
        self.stats = Stats()

        # ---- 预热数量：与冷启动耗时挂钩 ----
        self.t_create = self._calibrate(calibrate_samples)
        by_budget = math.ceil(warmup_budget / self.t_create) if self.t_create > 0 else max_size
        by_demand = math.ceil(expected_qps * self.t_create * headroom)
        self.min_idle = max(1, min(max_size, by_budget, by_demand))
        self._warmup()

    # ---------------- 预热 ----------------
    def _calibrate(self, samples: int) -> float:
        times = []
        for _ in range(samples):
            t0 = time.perf_counter()
            obj = self.factory()
            times.append(time.perf_counter() - t0)
            del obj
        times.sort()
        return max(times[len(times) // 2], 1e-9)

    def _warmup(self):
        t0 = time.perf_counter()
        now = time.monotonic()
        for _ in range(self.min_idle):
            self._idle.append(_Entry(obj=self.factory(), idle_since=now))
            self._size += 1
        self.warmup_seconds = time.perf_counter() - t0

    # ---------------- 借 ----------------
    def acquire(self) -> _Entry:
        s = self.stats
        s.borrows += 1
        # 无锁快路径
        if not self._sweeping:
            try:
                e = self._idle.popleft()
            except IndexError:
                e = None
            if e is not None:
                e.reuse += 1
                self._borrowed += 1
                s.hits += 1
                return e
        # 慢路径：新建 / 容量判定 / block 等待 / 等 sweep
        with self._mu:
            while True:
                while self._sweeping:
                    self._waiters += 1
                    self._cond.wait()
                    self._waiters -= 1
                try:
                    e = self._idle.popleft()
                    e.reuse += 1
                    self._borrowed += 1
                    s.hits += 1
                    return e
                except IndexError:
                    pass
                if self._size < self.max_size:
                    self._size += 1
                    self._borrowed += 1
                    s.misses += 1
                    return _Entry(obj=self.factory(), reuse=1)
                # 触及硬上界：超限策略
                s.overflows += 1
                if self.overflow == "discard":
                    s.misses += 1
                    return _Entry(obj=self.factory(), reuse=-1)  # 不受池管理
                if self.overflow == "raise":
                    raise PoolExhausted("pool at max_size and overflow='raise'")
                deadline = time.monotonic() + self.block_timeout
                self._waiters += 1
                while not self._idle and not self._sweeping:
                    if self._cond.wait(max(0.0, deadline - time.monotonic())) is False \
                            or time.monotonic() >= deadline:
                        self._waiters -= 1
                        raise PoolExhausted("block timeout waiting for an object")
                self._waiters -= 1

    # ---------------- 还 ----------------
    def release(self, entry: _Entry):
        s = self.stats
        s.releases += 1
        if entry.reuse == -1:                # 超限临时对象：即还即弃
            return
        ok = True
        try:
            if self.reset is not None:
                self.reset(entry.obj)
            elif hasattr(entry.obj, "reset"):
                entry.obj.reset()
        except Exception:
            ok = False
        if ok and self.verify_clean is not None:
            ok = bool(self.verify_clean(entry.obj))

        # entry 在归还期间归当前使用者独占，晋升无需加锁
        if ok:
            if entry.gen == GEN0 and entry.reuse >= self.promote_at:
                entry.gen = GEN1
            if entry.gen == GEN1 and entry.reuse >= self.tenure_at:
                entry.gen = GEN2
            entry.idle_since = time.monotonic()

        if not ok:
            with self._mu:
                self._size -= 1
                self._borrowed -= 1
                s.resets_failed += 1
                if self._waiters:
                    self._cond.notify()
            return

        self._borrowed -= 1
        if self._sweeping:                   # sweep 进行中：走锁保证不漏
            with self._mu:
                self._idle.append(entry)
                if self._waiters:
                    self._cond.notify()
        else:
            self._idle.append(entry)         # GIL 原子

        if next(self._sweep_tick) % self.sweep_every == 0:
            self.sweep()

    # ---------------- 分代回收 ----------------
    def _filter_locked(self, limit: int | None):
        """锁内：按 TTL 回收空闲对象，规模不低于 min_idle。"""
        now = time.monotonic()
        reclaimed_now = 0
        # 排空再放回（同一个 deque）；并发的 popleft/append 都是 GIL 原子，
        # 期间新还入的对象 idle_since=now，必不会被误回收。
        n = len(self._idle) if limit is None else min(limit, len(self._idle))
        popped: list[_Entry] = []
        for _ in range(n):
            try:
                popped.append(self._idle.popleft())
            except IndexError:
                break
        for e in popped:
            if self._size - reclaimed_now > self.min_idle \
                    and now - e.idle_since > self.ttl[e.gen]:
                reclaimed_now += 1
            else:
                self._idle.append(e)
        self._size -= reclaimed_now
        self.stats.reclaimed += reclaimed_now

    def sweep(self):
        with self._mu:
            if self._sweeping:
                return
            self._sweeping = True
            try:
                self._filter_locked(None)
            finally:
                self._sweeping = False
                self._cond.notify_all()

    # ---------------- 观测 ----------------
    def snapshot(self):
        with self._mu:
            g = [0, 0, 0]
            for e in self._idle:
                g[e.gen] += 1
            return {
                "size": self._size,
                "idle": len(self._idle),
                "borrowed": self._borrowed,
                "per_gen_idle": g,
                "hits": self.stats.hits,
                "misses": self.stats.misses,
                "overflows": self.stats.overflows,
                "reclaimed": self.stats.reclaimed,
                "resets_failed": self.stats.resets_failed,
                "hit_rate": round(self.stats.hit_rate, 6),
                "min_idle": self.min_idle,
            }

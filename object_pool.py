"""分代对象池（仅标准库）。

核心设计
========
1. 两代回收
   - 新生代 YOUNG：每次归还的对象先进新生代，带「最后归还时间戳」。
   - 老年代 OLD：新生代对象在一次 GC 周期后仍然「新鲜」（空闲未超过 young_ttl）
     则晋升老年代；老年代空闲超过 old_ttl 才回收。新生代回收频率高、阈值短，
     老年代阈值长，适合保存长期热点对象。
   - GC 是惰性触发的：每次 borrow/give_back 时检查距上次 GC 是否达到
     gc_interval，避免后台线程。

2. 借用顺序
   - 优先新生代（最近被使用，CPU/内核缓存更热），其次老年代，都没有才新建
     （记一次 miss）。

3. 容量上界
   - 空闲对象总数（young + old）恒 <= capacity。
   - 归还时若池满，对象被直接丢弃（dropped_full 计数），绝不无界增长。
   - 已借出未归还的对象不计入空闲池，由 outstanding / max_outstanding 指标
     暴露，便于泄漏告警。

4. 干净复用
   - give_back 时强制调用 reset(obj)；新建对象视为天然干净（不调 reset）。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Generic, Optional, TypeVar

T = TypeVar("T")

YOUNG = 0
OLD = 1


@dataclass
class PoolStats:
    """池运行指标，全部为单调累计量（max_outstanding 除外）。"""

    borrows: int = 0          # 总借用次数
    hits: int = 0             # 从池中取到现成对象的次数
    misses: int = 0           # 池空、新建对象的次数
    created: int = 0          # 累计新建对象数（含预热）
    prewarmed: int = 0        # 预热创建数
    evicted_young: int = 0    # 新生代空闲淘汰数
    evicted_old: int = 0      # 老年代空闲淘汰数
    promoted: int = 0         # 新生代 -> 老年代晋升数
    dropped_full: int = 0     # 池满丢弃的归还对象数
    reset_errors: int = 0     # reset 抛错的次数（出错对象按丢弃处理）
    gc_runs: int = 0          # GC 触发次数
    max_outstanding: int = 0  # 历史最大借出未还数

    @property
    def hit_rate(self) -> float:
        return self.hits / self.borrows if self.borrows else 0.0


class _Entry(Generic[T]):
    __slots__ = ("obj", "gen", "ts")

    def __init__(self, obj: T, gen: int, ts: float):
        self.obj = obj
        self.gen = gen
        self.ts = ts


class GenerationalPool(Generic[T]):
    def __init__(
        self,
        factory: Callable[[], T],
        reset: Optional[Callable[[T], None]] = None,
        *,
        capacity: int = 1024,
        prewarm_count: int = 0,
        young_ttl: float = 5.0,
        old_ttl: float = 60.0,
        gc_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if young_ttl <= 0 or old_ttl <= 0 or gc_interval <= 0:
            raise ValueError("ttl/gc_interval must be > 0")
        if old_ttl < young_ttl:
            raise ValueError("old_ttl must be >= young_ttl")
        self._factory = factory
        self._reset = reset
        self._capacity = capacity
        self.young_ttl = float(young_ttl)
        self.old_ttl = float(old_ttl)
        self.gc_interval = float(gc_interval)
        self._clock = clock

        self._young: deque[_Entry[T]] = deque()
        self._old: deque[_Entry[T]] = deque()
        self._outstanding = 0
        self._last_gc = clock()
        self.stats = PoolStats()

        if prewarm_count:
            self.prewarm(prewarm_count)

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def outstanding(self) -> int:
        """当前借出未归还的对象数。"""
        return self._outstanding

    def idle_count(self) -> int:
        return len(self._young) + len(self._old)

    def generation_counts(self) -> tuple[int, int]:
        """返回 (新生代空闲数, 老年代空闲数)。"""
        return len(self._young), len(self._old)

    def prewarm(self, n: int) -> None:
        """同步预热：新建 n 个对象并调 reset 完成初始化，放入新生代。

        预热 = 创建 + 初始化：对象被真正"点亮"（内存页已触、缓存已热），
        借出时不再付冷启动代价。受 capacity 上界约束；reset 抛错直接
        向上抛出（启动期 fail-fast）。
        """
        n = max(0, min(n, self._capacity - self.idle_count()))
        now = self._clock()
        for _ in range(n):
            obj = self._factory()
            if self._reset is not None:
                self._reset(obj)
            self._young.append(_Entry(obj, YOUNG, now))
        self.stats.created += n
        self.stats.prewarmed += n

    def borrow(self) -> T:
        now = self._clock()
        self._maybe_gc(now)
        s = self.stats
        s.borrows += 1

        if self._young:
            entry = self._young.pop()
            s.hits += 1
        elif self._old:
            entry = self._old.pop()
            s.hits += 1
        else:
            entry = None
            s.misses += 1
            s.created += 1

        self._outstanding += 1
        if self._outstanding > s.max_outstanding:
            s.max_outstanding = self._outstanding
        return entry.obj if entry is not None else self._factory()

    def give_back(self, obj: T) -> None:
        """归还对象：强制 reset；池满则丢弃。reset 失败的对象也丢弃。"""
        self._outstanding -= 1
        now = self._clock()
        self._maybe_gc(now)

        if self._reset is not None:
            try:
                self._reset(obj)
            except Exception:
                self.stats.reset_errors += 1
                return

        if self.idle_count() >= self._capacity:
            self.stats.dropped_full += 1
            return
        self._young.append(_Entry(obj, YOUNG, now))

    def force_gc(self) -> None:
        """显式触发一次 GC（自测/运维用）。"""
        self._gc(self._clock())

    def _maybe_gc(self, now: float) -> None:
        if now - self._last_gc >= self.gc_interval:
            self._gc(now)

    def _gc(self, now: float) -> None:
        self._last_gc = now
        self.stats.gc_runs += 1

        # 新生代：空闲超时的淘汰；其余晋升老年代。
        if self._young:
            survivors: deque[_Entry[T]] = deque()
            promoted = 0
            evicted = 0
            while self._young:
                e = self._young.popleft()
                if now - e.ts >= self.young_ttl:
                    evicted += 1
                else:
                    e.gen = OLD
                    e.ts = now  # 晋升时刻刷新计时：老年代计时从晋升起算
                    survivors.append(e)
                    promoted += 1
            self._old.extend(survivors)
            self.stats.promoted += promoted
            self.stats.evicted_young += evicted

        # 老年代：空闲超时淘汰。
        if self._old:
            keep: deque[_Entry[T]] = deque()
            evicted_old = 0
            while self._old:
                e = self._old.popleft()
                if now - e.ts >= self.old_ttl:
                    evicted_old += 1
                else:
                    keep.append(e)
            self._old = keep
            self.stats.evicted_old += evicted_old

    def leak_report(self) -> dict:
        return {
            "outstanding": self._outstanding,
            "max_outstanding": self.stats.max_outstanding,
            "created": self.stats.created,
            "returned_to_pool": self.stats.borrows
            - self._outstanding
            - self.stats.reset_errors
            - self.stats.dropped_full
            - self.stats.evicted_young
            - self.stats.evicted_old,
        }

"""事件循环公平性检查器（仅标准库）。

核心抽象
--------
- :class:`FakeClock`：测试可注入的时钟（时间单位：纳秒整数）。
- :class:`EventLoop`：单线程事件循环，支持在任意时刻注入真实时钟
  （``time.monotonic_ns`` + ``time.sleep``）或测试时钟。
- :class:`FairnessMonitor`：挂在循环上的监控器。每次回调执行只做 O(1)
  工作，分位数由定长（64 桶）对数直方图计算，因此内存占用只与
  “不同回调种类数”有关，与回调总次数无关。
- :func:`analyze` / :class:`FairnessReport`：对监控器（或一段离线记录）
  做纯函数式分析，同一份记录重复分析结果必然一致。

所有时间均为纳秒整数，避免浮点误差导致的不一致。
"""

from __future__ import annotations

import heapq
import itertools
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 时间源
# ---------------------------------------------------------------------------


class FakeClock:
    """测试注入用的虚拟时钟。``advance`` 只会把时钟向前拨。"""

    __slots__ = ("now_ns",)

    def __init__(self, start_ns: int = 0) -> None:
        self.now_ns = int(start_ns)

    def time_ns(self) -> int:
        return self.now_ns

    def advance(self, ns: int) -> None:
        if ns < 0:
            raise ValueError("时钟不能回拨: %r" % ns)
        self.now_ns += int(ns)


# ---------------------------------------------------------------------------
# 定长对数直方图：分位数估计，内存与样本数无关
# ---------------------------------------------------------------------------

BUCKET_COUNT = 64
# 桶 i（i>=1）覆盖 [2^(i-1), 2^i) 纳秒；桶 0 覆盖 [0,1) 纳秒。
# 最大可表示 2^63 ns ≈ 292 年。


def _bucket_index(value_ns: int) -> int:
    if value_ns <= 0:
        return 0
    idx = value_ns.bit_length()  # 2^(idx-1) <= v < 2^idx
    if idx >= BUCKET_COUNT:
        return BUCKET_COUNT - 1
    return idx


def _bucket_lower_bound(idx: int) -> int:
    return 0 if idx == 0 else 1 << (idx - 1)


class Histogram:
    """对数分桶直方图。add 为 O(1)，percentile 为 O(桶数)。"""

    __slots__ = ("buckets", "count", "total", "maximum")

    def __init__(self) -> None:
        self.buckets: List[int] = [0] * BUCKET_COUNT
        self.count: int = 0
        self.total: int = 0
        self.maximum: int = 0

    def add(self, value_ns: int) -> None:
        self.buckets[_bucket_index(value_ns)] += 1
        self.count += 1
        self.total += value_ns
        if value_ns > self.maximum:
            self.maximum = value_ns

    def mean(self) -> int:
        return self.total // self.count if self.count else 0

    def percentile(self, q: float) -> int:
        """返回 q 分位数（0~100）。

        取“累计样本数首次达到 ceil(q/100*N) 的那个桶”的**下界**，
        因此是真实分位数的保守下界，确定且可复现（不做桶内插值）。
        """
        if not 0.0 <= q <= 100.0:
            raise ValueError("分位数必须在 [0, 100] 内: %r" % q)
        if self.count == 0:
            return 0
        target = (self.count * int(round(q * 100)) + 9999) // 10000
        if q == 0:
            target = 1
        cumulative = 0
        for idx, c in enumerate(self.buckets):
            cumulative += c
            if cumulative >= target:
                return _bucket_lower_bound(idx)
        return _bucket_lower_bound(BUCKET_COUNT - 1)


# ---------------------------------------------------------------------------
# 监控器：在线采集，每回调 O(1)
# ---------------------------------------------------------------------------

CallbackKey = Tuple[str, str]  # (回调名, 注册点 "file:line")


@dataclass
class CallbackStats:
    """同一个注册点的聚合计数。所有值均为纳秒整数。"""

    name: str
    site: str
    invocations: int = 0
    total_block_ns: int = 0
    max_block_ns: int = 0
    total_delay_ns: int = 0
    max_delay_ns: int = 0


@dataclass
class MetricSummary:
    count: int = 0
    total_ns: int = 0
    max_ns: int = 0
    mean_ns: int = 0
    p50_ns: int = 0
    p90_ns: int = 0
    p99_ns: int = 0


class FairnessMonitor:
    """挂载到 :class:`EventLoop` 的公平性监控器。

    每次回调 begin/end 只做常数次整数运算和两次字典操作；
    分位数由定长直方图统计，不保存逐事件明细。
    """

    def __init__(self, time_fn: Callable[[], int]) -> None:
        self._time_fn = time_fn
        self.block_hist = Histogram()
        self.delay_hist = Histogram()
        self._stats: Dict[CallbackKey, CallbackStats] = {}
        self._open: Dict[int, Tuple[CallbackKey, int, int]] = {}
        self._seq = 0
        self.events = 0
        self.idle_ns = 0
        self.first_start_ns: Optional[int] = None
        self.last_end_ns: Optional[int] = None

    # --- 事件循环回调 ---------------------------------------------------

    def begin(self, key: CallbackKey, scheduled_ns: int) -> int:
        started_ns = self._time_fn()
        token = self._seq
        self._seq += 1
        self._open[token] = (key, scheduled_ns, started_ns)
        if self.first_start_ns is None:
            self.first_start_ns = started_ns
        return token

    def end(self, token: int) -> None:
        ended_ns = self._time_fn()
        key, scheduled_ns, started_ns = self._open.pop(token)
        block_ns = ended_ns - started_ns
        delay_ns = started_ns - scheduled_ns  # 负延迟（提前触发）截断为 0
        if delay_ns < 0:
            delay_ns = 0

        stats = self._stats.get(key)
        if stats is None:
            stats = CallbackStats(name=key[0], site=key[1])
            self._stats[key] = stats
        stats.invocations += 1
        stats.total_block_ns += block_ns
        stats.total_delay_ns += delay_ns
        if block_ns > stats.max_block_ns:
            stats.max_block_ns = block_ns
        if delay_ns > stats.max_delay_ns:
            stats.max_delay_ns = delay_ns

        self.block_hist.add(block_ns)
        self.delay_hist.add(delay_ns)
        self.events += 1
        self.last_end_ns = ended_ns

    def record_idle(self, idle_ns: int) -> None:
        if idle_ns > 0:
            self.idle_ns += idle_ns

    # --- 分析入口 -------------------------------------------------------

    def report(self) -> "FairnessReport":
        return analyze(self)


def _summarize(h: Histogram) -> MetricSummary:
    return MetricSummary(
        count=h.count,
        total_ns=h.total,
        max_ns=h.maximum,
        mean_ns=h.mean(),
        p50_ns=h.percentile(50),
        p90_ns=h.percentile(90),
        p99_ns=h.percentile(99),
    )


@dataclass
class Offender:
    """拖累清单中的一行。"""

    name: str
    site: str
    invocations: int
    total_block_ns: int
    max_block_ns: int
    mean_block_ns: int
    max_delay_ns: int
    block_share: float  # 占全部回调占用时间的比例 [0,1]


@dataclass
class FairnessReport:
    events: int
    wall_ns: int
    busy_ns: int
    idle_ns: int
    idle_share: float
    block: MetricSummary
    delay: MetricSummary
    offenders: List[Offender] = field(default_factory=list)

    def to_dict(self) -> dict:
        """确定性的纯数据表示，可直接对拍（JSON 序列化也稳定）。"""
        return {
            "events": self.events,
            "wall_ns": self.wall_ns,
            "busy_ns": self.busy_ns,
            "idle_ns": self.idle_ns,
            "idle_share": round(self.idle_share, 12),
            "block": self.block.__dict__,
            "delay": self.delay.__dict__,
            "offenders": [
                {
                    "name": o.name,
                    "site": o.site,
                    "invocations": o.invocations,
                    "total_block_ns": o.total_block_ns,
                    "max_block_ns": o.max_block_ns,
                    "mean_block_ns": o.mean_block_ns,
                    "max_delay_ns": o.max_delay_ns,
                    "block_share": round(o.block_share, 12),
                }
                for o in self.offenders
            ],
        }


def analyze(monitor: FairnessMonitor) -> FairnessReport:
    """对监控器采集到的记录做分析。纯函数：相同输入必然相同输出。"""
    busy_ns = monitor.block_hist.total
    first = monitor.first_start_ns
    last = monitor.last_end_ns
    wall_ns = (last - first) if (first is not None and last is not None) else 0

    rows: List[Offender] = []
    for stats in monitor._stats.values():
        mean_block = (
            stats.total_block_ns // stats.invocations if stats.invocations else 0
        )
        share = (stats.total_block_ns / busy_ns) if busy_ns else 0.0
        rows.append(
            Offender(
                name=stats.name,
                site=stats.site,
                invocations=stats.invocations,
                total_block_ns=stats.total_block_ns,
                max_block_ns=stats.max_block_ns,
                mean_block_ns=mean_block,
                max_delay_ns=stats.max_delay_ns,
                block_share=share,
            )
        )
    # 排序口径：总占用降序 → 单次最大降序 → 名称/注册点（保证全序、可复现）
    rows.sort(
        key=lambda o: (-o.total_block_ns, -o.max_block_ns, o.name, o.site)
    )

    return FairnessReport(
        events=monitor.events,
        wall_ns=wall_ns,
        busy_ns=busy_ns,
        idle_ns=monitor.idle_ns,
        idle_share=(monitor.idle_ns / wall_ns) if wall_ns else 0.0,
        block=_summarize(monitor.block_hist),
        delay=_summarize(monitor.delay_hist),
        offenders=rows,
    )


# ---------------------------------------------------------------------------
# 被监控的事件循环
# ---------------------------------------------------------------------------


def _caller_site(frames_up: int = 1) -> str:
    """返回调用方注册点 "文件:行号"。用 sys._getframe，O(1) 无 IO。"""
    frame = sys._getframe(frames_up + 1)
    return "%s:%d" % (frame.f_code.co_filename, frame.f_lineno)


class EventLoop:
    """极简单线程事件循环，演示监控点的接入方式。

    定时器用最小堆按 (计划时刻, 入队序号) 排序，先到期先执行；
    同一时刻的回调保持注册顺序。

    参数
    ----
    time_fn: 返回当前单调时间（纳秒）的可调用对象，由调用方注入。
    advance_fn: 需要“等待”到未来时刻时调用，参数为等待纳秒数。
        真实运行可传 ``lambda ns: time.sleep(ns / 1e9)``；
        配合 :class:`FakeClock` 测试时传 ``clock.advance``。
    monitor: 可选的 :class:`FairnessMonitor`；不传则零监控开销。
    """

    def __init__(
        self,
        time_fn: Callable[[], int],
        advance_fn: Optional[Callable[[int], None]] = None,
        monitor: Optional[FairnessMonitor] = None,
    ) -> None:
        self._time_fn = time_fn
        self._advance_fn = advance_fn or (lambda _ns: None)
        self._monitor = monitor
        self._heap: List[Tuple[int, int, CallbackKey, Callable[[], None]]] = []
        self._seq = itertools.count()

    @property
    def monitor(self) -> Optional[FairnessMonitor]:
        return self._monitor

    def call_later(
        self,
        delay_ns: int,
        callback: Callable[[], None],
        name: Optional[str] = None,
        site: Optional[str] = None,
    ) -> None:
        if site is None:
            site = _caller_site(1)
        key = (name or getattr(callback, "__name__", "<anonymous>"), site)
        heapq.heappush(
            self._heap,
            (self._time_fn() + int(delay_ns), next(self._seq), key, callback),
        )

    def call_soon(
        self,
        callback: Callable[[], None],
        name: Optional[str] = None,
        site: Optional[str] = None,
    ) -> None:
        if site is None:
            site = _caller_site(1)
        self.call_later(0, callback, name=name, site=site)

    def run(self) -> None:
        monitor = self._monitor
        heap = self._heap
        while heap:
            scheduled_ns, _seq, key, callback = heapq.heappop(heap)
            now_ns = self._time_fn()
            if scheduled_ns > now_ns:
                wait_ns = scheduled_ns - now_ns
                if monitor is not None:
                    monitor.record_idle(wait_ns)
                self._advance_fn(wait_ns)
            if monitor is None:
                callback()
                continue
            token = monitor.begin(key, scheduled_ns)
            try:
                callback()
            finally:
                monitor.end(token)

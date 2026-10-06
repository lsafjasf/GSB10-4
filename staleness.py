"""统计信息过期检测库（仅 Python 标准库）。

判定依据（三路信号，信号不足时显式降级为 UNKNOWN/更保守）：
  1. 变更量 change_ratio = max(|inserts-deletes|, updates) / 快照时行数
     —— 用净行数变化（计数器可精确推算），不用 churn 上界；均衡增删不改统计，
        上界会系统性误判。updates 单独计入（净行数不变但内容可能变了）。
  2. 分布漂移 drift = 抽样直方图与快照直方图的 TV 距离（total variation）
     —— 捕获"行数没变但数据分布变了"的情形（如批量 UPDATE）。
  3. 时间 age_days = 距统计计算的天数
     —— 弱先验：时间到了不一定过期，只能作为辅助项，权重最低。

权衡方式（关键：计数器是粗上界，均衡的增删 churn 可能根本不改分布，
          所以"有探针时"以直接分布证据为主；时间永不硬触发）：
  - 每路信号归一化到 [0,1]：s_x = min(实测值 / 该路硬阈值, 1)。
  - 有分布探针（推荐）：权重 变更量0.30 / 漂移0.55 / 时间0.15；
      仅漂移 >= 漂移硬阈值 才硬判 STALE（分布确实变了的直接证据）；
      变更量只抬分，不能单独定罪，避免 churn 上界造成的误判。
  - 无分布探针：权重在 变更量/时间 间重归一化（约 0.70/0.30）；
      变更量 >= 变更硬阈值 时硬判 STALE（此时计数器是唯一实质证据）。
  - 时间永远不硬触发："时间到了"不等于过期（题目核心诉求），只作弱先验抬分。
  - 判 STALE 的条件：硬触发，或综合分 score >= 综合阈值（默认0.5）。
  - 无快照（统计信息缺失）直接返回 UNKNOWN，绝不默认新鲜。
"""
from __future__ import annotations

import random
import time
import zlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional


class Verdict(Enum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass
class Thresholds:
    change_ratio: float = 0.20   # 变更量硬阈值：变更行数占快照行数 20%
    drift: float = 0.25          # 分布漂移硬阈值：TV 距离 0.25
    age_days: float = 30.0       # 时间硬阈值：30 天
    score: float = 0.50          # 综合分阈值
    w_change: float = 0.30  # 有探针时；无探针自动重归一化
    w_drift: float = 0.55
    w_time: float = 0.15


@dataclass
class StatsSnapshot:
    computed_at: float
    row_count: int
    histogram: Dict[int, int]    # bucket -> 行数
    lo: Optional[float] = None   # 快照时列最小值（直方图下界）
    hi: Optional[float] = None   # 快照时列最大值（直方图上界）


@dataclass
class ChangeCounter:
    inserts: int = 0
    updates: int = 0
    deletes: int = 0

    @property
    def total(self) -> int:
        return self.inserts + self.updates + self.deletes


@dataclass
class Decision:
    table: str
    verdict: Verdict
    score: Optional[float]           # UNKNOWN 时为 None
    reasons: List[str]
    signals: Dict[str, Optional[float]]  # change_ratio / drift / age_days


def total_variation(hist_a: Dict[int, int], hist_b: Dict[int, int]) -> float:
    """两个直方图的 TV 距离，0=完全相同，1=完全不相交。"""
    sum_a = sum(hist_a.values())
    sum_b = sum(hist_b.values())
    if sum_a == 0 or sum_b == 0:
        return 1.0 if sum_a != sum_b else 0.0
    keys = set(hist_a) | set(hist_b)
    return 0.5 * sum(
        abs(hist_a.get(k, 0) / sum_a - hist_b.get(k, 0) / sum_b) for k in keys
    )


class StalenessDetector:
    def __init__(
        self,
        thresholds: Optional[Thresholds] = None,
        buckets: int = 16,
        probe_size: int = 256,
        bucket_fn: Optional[Callable[[object], int]] = None,
        rng: Optional[random.Random] = None,
    ):
        self.th = thresholds or Thresholds()
        self.buckets = buckets
        self.probe_size = probe_size
        # 默认分桶：整数用乘法散列（快），其他类型用 crc32。
        # 两者都能捕获值域整体平移（v%n 在平移量为桶数整数倍时会漏检）。
        self.bucket_fn = bucket_fn or self._default_bucket
        self.rng = rng or random.Random(0)
        self.snapshots: Dict[str, StatsSnapshot] = {}
        self.counters: Dict[str, ChangeCounter] = {}

    # ---- 状态维护 ----
    def record_snapshot(
        self, table: str, row_count: int, histogram: Dict[int, int],
        computed_at: Optional[float] = None,
        lo: Optional[float] = None, hi: Optional[float] = None,
    ) -> None:
        self.snapshots[table] = StatsSnapshot(
            computed_at if computed_at is not None else time.time(),
            row_count, dict(histogram), lo, hi,
        )
        self.counters[table] = ChangeCounter()

    def record_changes(self, table: str, inserts: int = 0,
                       updates: int = 0, deletes: int = 0) -> None:
        c = self.counters.setdefault(table, ChangeCounter())
        c.inserts += inserts
        c.updates += updates
        c.deletes += deletes

    def _default_bucket(self, v) -> int:
        if isinstance(v, int):
            return (v * 2654435761 & 0xFFFFFFFF) % self.buckets
        return zlib.crc32(str(v).encode()) % self.buckets

    def build_histogram(self, rows) -> Dict[int, int]:
        hist: Dict[int, int] = {}
        for v in rows:
            b = self.bucket_fn(v)
            hist[b] = hist.get(b, 0) + 1
        return hist

    # ---- 判定 ----
    def check(self, table: str, now: Optional[float] = None,
              probe_rows=None) -> Decision:
        now = now if now is not None else time.time()
        snap = self.snapshots.get(table)
        if snap is None:
            return Decision(
                table, Verdict.UNKNOWN, None,
                ["统计信息缺失（无快照），无法判断，不默认当作新鲜"],
                {"change_ratio": None, "drift": None, "age_days": None},
            )

        counter = self.counters.get(table, ChangeCounter())
        net_rows = abs(counter.inserts - counter.deletes)
        change_ratio = max(net_rows, counter.updates) / max(snap.row_count, 1)
        age_days = max(0.0, (now - snap.computed_at) / 86400.0)

        drift: Optional[float] = None
        if probe_rows is not None:
            # 可索引序列按索引抽样，真正 O(probe_size)，不复制全表；
            # 不可索引（如游标迭代器）才退化为物化采样。
            if (hasattr(probe_rows, "__len__")
                    and hasattr(probe_rows, "__getitem__")):
                n = len(probe_rows)
                if n > self.probe_size:
                    sample = [probe_rows[i] for i in
                              self.rng.sample(range(n), self.probe_size)]
                else:
                    sample = list(probe_rows)
            else:
                sample = list(probe_rows)
                if len(sample) > self.probe_size:
                    sample = self.rng.sample(sample, self.probe_size)
            if sample:
                tv = total_variation(snap.histogram,
                                     self.build_histogram(sample))
                # 越界率：抽样值落在快照 [lo,hi] 之外的比例，
                # 捕获"直方图形状没变但值域整体迁移"（对应真实统计的直方图边界失效）
                outside = 0.0
                if snap.lo is not None and snap.hi is not None:
                    outside = sum(1 for v in sample
                                  if v < snap.lo or v > snap.hi) / len(sample)
                drift = max(tv, outside)

        signals = {"change_ratio": change_ratio, "drift": drift, "age_days": age_days}

        # 归一化子分数；有探针时以漂移为主，无探针时计数器可硬触发；
        # 时间只抬分，永不硬触发。
        parts, weights, reasons = [], [], []
        hard = False
        s_c = min(change_ratio / self.th.change_ratio, 1.0)
        s_t = min(age_days / self.th.age_days, 1.0)
        parts.append(s_c); weights.append(self.th.w_change)
        parts.append(s_t); weights.append(self.th.w_time)
        # 净行数变化是精确信号（非上界），净变化超阈值必然改变基数类统计，
        # 无论有无探针都硬触发；漂移硬触发仅在有探针时可用。
        if change_ratio >= self.th.change_ratio:
            hard = True
            reasons.append(
                f"净变更量 {change_ratio:.1%} >= 硬阈值 "
                f"{self.th.change_ratio:.0%}（基数统计必然失效）")
        if drift is not None:
            s_d = min(drift / self.th.drift, 1.0)
            parts.append(s_d); weights.append(self.th.w_drift)
            if drift >= self.th.drift:
                hard = True
                reasons.append(
                    f"分布漂移 TV={drift:.3f} >= 硬阈值 {self.th.drift}（直接证据）")
        else:
            reasons.append("无分布探针，漂移未知，变更量权重上调、结论更保守")

        wsum = sum(weights)
        score = sum(p * w for p, w in zip(parts, weights)) / wsum

        if hard or score >= self.th.score:
            if not reasons:
                reasons.append(f"综合分 {score:.3f} >= 阈值 {self.th.score}")
            verdict = Verdict.STALE
        else:
            reasons.append(f"综合分 {score:.3f} < 阈值 {self.th.score}，无硬触发")
            verdict = Verdict.FRESH
        return Decision(table, verdict, score, reasons, signals)

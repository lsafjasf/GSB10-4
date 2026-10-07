"""统计信息过期检测库（仅标准库）。

三路信号：
  1. 变更量 change_ratio = (inserts+updates+deletes) / 统计时的行数
     —— 粗粒度、保守；同分布换血时它 alone 会误报，所以只作兜底。
  2. 分布漂移 drift_psi
     —— 用「幸存旧行 x 快照直方图 + 新写入行 x 蓄水池样本直方图」加权
        估计当前全表分布，再与快照求 PSI。样本只来自写流，必须加权，
        否则少量写入也会被放大成全表漂移。
  3. 时间 age = now - 统计时刻
     —— 兜底：覆盖计数器/采样都漏掉的缓慢泄漏与未跟踪写路径。

判定规则（decide）：
  - 快照缺失                     -> UNKNOWN（显式无法判断，绝不默认新鲜）
  - age 超期                     -> STALE（时间兜底，独立触发）
  - 漂移信号可用：
      drift >= psi 阈值          -> STALE
      drift 低于阈值（veto 开启） -> 仅当净行数变化超限时 STALE，否则 FRESH
  - 漂移信号不可用（样本太少）    -> 回退变更量阈值
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

N_BINS = 10          # 直方图等宽桶数（另有下溢/上溢 2 桶）
_PSI_EPS = 1e-4      # PSI 平滑项，避免除零

FRESH = "FRESH"
STALE = "STALE"
UNKNOWN = "UNKNOWN"


def psi(expected: List[float], actual: List[float]) -> float:
    """Population Stability Index：两组归一化直方图的分布距离。"""
    total = 0.0
    for e, a in zip(expected, actual):
        e = max(e, _PSI_EPS)
        a = max(a, _PSI_EPS)
        total += (a - e) * math.log(a / e)
    return total


class Histogram:
    """等宽直方图，桶边界在 analyze 时固定；范围外的值落入下溢/上溢桶，
    因此分布整体平移也能被上溢桶捕获。"""

    def __init__(self, values, n_bins: int = N_BINS):
        values = list(values)
        lo, hi = min(values), max(values)
        if hi <= lo:
            hi = lo + 1.0
        self.lo, self.hi, self.n_bins = lo, hi, n_bins
        self.frac = self._fractions(values)

    def _fractions(self, values) -> List[float]:
        width = (self.hi - self.lo) / self.n_bins
        counts = [0] * (self.n_bins + 2)  # [下溢] + n_bins + [上溢]
        for v in values:
            if v < self.lo:
                counts[0] += 1
            elif v >= self.hi:
                counts[-1] += 1
            else:
                counts[1 + int((v - self.lo) / width)] += 1
        total = sum(counts) or 1
        return [c / total for c in counts]

    def fractions_of(self, values) -> List[float]:
        return self._fractions(values)


@dataclass
class Snapshot:
    taken_at: int          # 逻辑时钟刻度
    row_count: int
    hist: Histogram


@dataclass
class TableState:
    """每张表需要维护的轻量元数据：快照 + 变更计数 + 蓄水池样本。"""
    name: str
    snapshot: Optional[Snapshot] = None
    inserts: int = 0
    updates: int = 0
    deletes: int = 0
    sample: List[float] = field(default_factory=list)
    sample_seen: int = 0
    sample_cap: int = 128

    def record_write(self, value: float, rng) -> None:
        """记录一次 insert/update 的新值（算法 R 蓄水池采样，O(1)）。"""
        self.sample_seen += 1
        if len(self.sample) < self.sample_cap:
            self.sample.append(value)
        else:
            j = rng.randrange(self.sample_seen)
            if j < self.sample_cap:
                self.sample[j] = value

    def record_delete(self) -> None:
        self.deletes += 1


@dataclass
class Thresholds:
    change_ratio: float = 0.20   # 兜底：累计变更量 / 统计时行数
    psi: float = 0.25            # PSI >= 0.25 视为显著漂移（业界常用界）
    max_age: int = 100           # 快照最大存活刻度（时间兜底）
    row_change: float = 0.10     # 净行数变化超限即过期（行数也是统计信息）
    min_sample: int = 30         # 样本不足此数则漂移信号不可用
    drift_veto: bool = True      # 漂移证据充分时，允许其否决变更量告警


@dataclass
class Signals:
    change_ratio: Optional[float]   # 总变更量 / 统计时行数
    row_change: Optional[float]     # 净行数变化率 (inserts-deletes)/行数
    drift_psi: Optional[float]      # 加权估计分布 vs 快照的 PSI
    age: Optional[int]


@dataclass
class Verdict:
    status: str                  # FRESH / STALE / UNKNOWN
    reasons: List[str]
    signals: Signals


class Detector:
    def __init__(self, thresholds: Thresholds = Thresholds()):
        self.th = thresholds

    def signals(self, tbl: TableState, now: int) -> Signals:
        snap = tbl.snapshot
        assert snap is not None
        base = max(snap.row_count, 1)
        change_ratio = (tbl.inserts + tbl.updates + tbl.deletes) / base
        row_change = (tbl.inserts - tbl.deletes) / base
        drift = None
        if len(tbl.sample) >= self.th.min_sample:
            # 当前分布估计 = 幸存旧行*快照 + 新写入行*样本
            surviving = max(snap.row_count - tbl.deletes - tbl.updates, 0)
            new_rows = tbl.inserts + tbl.updates
            total = surviving + new_rows
            if total > 0:
                w_old = surviving / total
                w_new = new_rows / total
                sample_frac = snap.hist.fractions_of(tbl.sample)
                est = [w_old * e + w_new * s
                       for e, s in zip(snap.hist.frac, sample_frac)]
                drift = psi(snap.hist.frac, est)
        age = now - snap.taken_at
        return Signals(change_ratio, row_change, drift, age)

    def decide(self, tbl: TableState, sig: Signals) -> Verdict:
        th = self.th
        if sig.age is not None and sig.age >= th.max_age:
            return Verdict(STALE, [f"age={sig.age} >= {th.max_age} (时间兜底)"],
                           sig)
        if sig.drift_psi is not None:
            if sig.drift_psi >= th.psi:
                return Verdict(
                    STALE,
                    [f"drift_psi={sig.drift_psi:.3f} >= {th.psi}"], sig)
            if th.drift_veto:
                # 有充分采样证据证明分布未漂移；只有行数本身变化超限才算过期
                if sig.row_change is not None and abs(sig.row_change) >= th.row_change:
                    return Verdict(
                        STALE,
                        [f"row_change={sig.row_change:+.3f} 超出 "
                         f"+-{th.row_change}（分布未漂移，但行数统计已失真）"],
                        sig)
                return Verdict(
                    FRESH,
                    [f"drift_psi={sig.drift_psi:.3f} < {th.psi}，"
                     f"分布证据否决变更量告警"
                     f"(change_ratio={sig.change_ratio:.3f})"], sig)
            # veto 关闭：三路简单或逻辑
            if sig.change_ratio is not None and sig.change_ratio >= th.change_ratio:
                return Verdict(
                    STALE,
                    [f"change_ratio={sig.change_ratio:.3f} >= "
                     f"{th.change_ratio}"], sig)
            return Verdict(FRESH, ["all signals below thresholds"], sig)
        # 漂移信号不可用（样本不足）：变更量兜底
        if sig.change_ratio is not None and sig.change_ratio >= th.change_ratio:
            return Verdict(
                STALE,
                [f"change_ratio={sig.change_ratio:.3f} >= {th.change_ratio} "
                 f"(样本不足，变更量兜底)"], sig)
        note = "all signals below thresholds"
        if (tbl.inserts + tbl.updates) > 0:
            note = ("drift signal unavailable: sample too small, "
                    "judged by change_ratio+age only")
        return Verdict(FRESH, [note], sig)

    def assess(self, tbl: TableState, now: int) -> Verdict:
        if tbl.snapshot is None:
            # 显式无法判断：没有统计快照，任何“新鲜”结论都是猜测
            return Verdict(UNKNOWN,
                           ["no statistics snapshot; cannot judge staleness"],
                           Signals(None, None, None, None))
        sig = self.signals(tbl, now)
        return self.decide(tbl, sig)

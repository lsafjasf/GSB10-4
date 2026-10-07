"""覆盖索引 vs 回表：代价模型、执行引擎模拟与反馈修正（仅标准库）。

组成：
- Engine    模拟存储引擎：堆页 + 有序二级索引 + LRU 缓冲池。
            执行计划并给出【实测】耗时（实测 CPU + 模拟页故障计费）与实测内存。
- Histogram 等深直方图，用于范围选择率估算。
- TableStats 表/索引统计信息：行数、行宽、不同值个数、直方图、聚簇度。
- CostModel 基于选择率 / 行宽 / 排序要求的代价估算，枚举候选计划并选优；
            统计信息缺失时显式报告并退回保守计划。
- calibrate 用实测样本对代价常数做最小二乘修正（偏差过大时的修正机制）。
"""

from __future__ import annotations

import bisect
import math
import time
import tracemalloc
from collections import OrderedDict
from dataclasses import dataclass

PAGE_SIZE = 8192            # 页大小（字节）
POOL_PAGES = 256            # 缓冲池页数
SEQ_PAGE_US = 5.0           # 引擎“真实”顺序页读延迟（模拟磁盘，微秒）
RAND_PAGE_US = 50.0         # 引擎“真实”随机页读延迟（模拟磁盘，微秒）

FULL_SCAN = "full_scan"
INDEX_COVERING = "index_covering"
INDEX_LOOKUP = "index_lookup"   # 回表

# 代价特征向量维度（估算与实测共用同一套特征，便于最小二乘修正）
FEATURES = ("seq_pages", "rand_pages", "index_rows",
            "scan_rows", "sort_nlogn", "out_rows")


# ---------------------------------------------------------------- 统计信息

class Histogram:
    """等深直方图：桶内假设均匀分布，用于范围选择率估算。"""

    def __init__(self, sorted_keys, buckets=64):
        n = len(sorted_keys)
        self.buckets = max(1, min(buckets, n))
        step = n / self.buckets
        self.bounds = [sorted_keys[min(n - 1, int((i + 1) * step) - 1)]
                       for i in range(self.buckets)]
        self.lo = sorted_keys[0]
        self.hi = sorted_keys[-1]

    def _cdf(self, x):
        if x <= self.lo:
            return 0.0
        if x >= self.hi:
            return 1.0
        i = bisect.bisect_left(self.bounds, x)
        lower = self.bounds[i - 1] if i > 0 else self.lo
        upper = self.bounds[i]
        base = i / self.buckets
        if upper <= lower:
            return min(1.0, base + 1.0 / self.buckets)
        return min(1.0, base + (x - lower) / (upper - lower) / self.buckets)

    def selectivity(self, lo, hi):
        """谓词 lo <= key <= hi 的选择率，区间 [0,1]。"""
        return max(0.0, self._cdf(hi) - self._cdf(lo))


@dataclass
class TableStats:
    row_count: int          # 行数
    row_width: int          # 行宽（字节），决定每页行数
    n_distinct: int         # 索引列不同值个数
    histogram: Histogram    # 索引列直方图
    clustering: float       # 聚簇度 0..1：索引顺序与堆物理顺序的相关性

    @property
    def rows_per_page(self):
        return max(1, PAGE_SIZE // self.row_width)

    @property
    def num_pages(self):
        return math.ceil(self.row_count / self.rows_per_page)


# ---------------------------------------------------------------- 查询与计划

@dataclass
class Query:
    lo: float               # 范围谓词下界（索引列）
    hi: float               # 范围谓词上界
    covering: bool          # SELECT 列是否被索引完全覆盖
    order_by_index: bool = False   # 是否 ORDER BY 索引列
    proj_width: int = 64    # 投影行宽（字节），影响结果内存估算
    name: str = ""


@dataclass
class PlanEstimate:
    plan: str
    est_us: float           # 估算耗时（微秒）
    est_mem: float          # 估算内存（字节）
    features: dict          # 代价特征向量


@dataclass
class Decision:
    plan: str
    estimates: dict | None  # plan -> PlanEstimate；无法估算时为 None
    reason: str = ""        # 无法估算 / 保守选择的显式说明

    @property
    def estimated(self):
        return self.estimates is not None


@dataclass
class ExecResult:
    plan: str
    time_us: float          # 实测总耗时 = 实测 CPU + 模拟页故障计费
    cpu_us: float           # 其中实测 CPU 部分
    mem_bytes: float        # 实测内存峰值（tracemalloc）
    features: dict          # 实测特征向量（真实页故障数等）
    out_count: int


@dataclass
class CostConstants:
    """代价常数（微秒）。初值是粗估，calibrate() 用实测数据修正。"""
    seq_page_us: float = 12.0
    rand_page_us: float = 25.0
    index_row_us: float = 0.05
    scan_row_us: float = 0.20
    sort_nlogn_us: float = 0.10
    out_row_us: float = 0.10


# ---------------------------------------------------------------- 代价模型

class CostModel:
    """代价 = 各特征 x 常数 的线性组合；不靠任何固定阈值做决策。"""

    def __init__(self, constants=None, mem_factor=1.0):
        self.c = constants or CostConstants()
        self.mem_factor = mem_factor   # 内存估算的全局修正系数（反馈得到）

    def _features(self, plan, stats, sel, q):
        n = sel * stats.row_count                 # 估算命中行数
        rpp = stats.rows_per_page
        feats = dict.fromkeys(FEATURES, 0.0)
        if plan == FULL_SCAN:
            feats["seq_pages"] = float(stats.num_pages)
            feats["scan_rows"] = float(stats.row_count)
            feats["out_rows"] = n
            if q.order_by_index:
                # 全表扫描不满足排序要求，需显式排序 n*log2(n)
                feats["sort_nlogn"] = n * math.log2(max(2.0, n))
        elif plan == INDEX_COVERING:
            feats["index_rows"] = n               # 只读索引页，不回表
            feats["out_rows"] = n
        elif plan == INDEX_LOOKUP:
            feats["index_rows"] = n
            feats["out_rows"] = n
            # 回表随机读：Mackert-Lohman 公式估算触及的不同堆页数
            pages = stats.num_pages
            if n > 0 and pages > 0:
                distinct_pages = pages * (1.0 - (1.0 - 1.0 / pages) ** n)
            else:
                distinct_pages = 0.0
            cl = stats.clustering
            # 聚簇度插值：完全随机 -> distinct_pages；完全聚簇 -> n/rpp 顺序页
            feats["rand_pages"] = (min(n, distinct_pages) * (1.0 - cl)
                                   + (n / rpp) * cl)
        else:
            raise ValueError(plan)
        return feats

    def _cost_us(self, feats):
        c = self.c
        return (feats["seq_pages"] * c.seq_page_us
                + feats["rand_pages"] * c.rand_page_us
                + feats["index_rows"] * c.index_row_us
                + feats["scan_rows"] * c.scan_row_us
                + feats["sort_nlogn"] * c.sort_nlogn_us
                + feats["out_rows"] * c.out_row_us)

    def _mem_bytes(self, feats, q):
        mem = feats["out_rows"] * q.proj_width
        if feats["sort_nlogn"] > 0:
            mem *= 2.0                            # 排序需额外临时缓冲
        if feats["rand_pages"] > 0:
            mem += feats["index_rows"] * 16.0     # 回表需 rowid 缓冲
        return mem * self.mem_factor

    def estimate(self, plan, q, stats):
        sel = stats.histogram.selectivity(q.lo, q.hi)
        feats = self._features(plan, stats, sel, q)
        return PlanEstimate(plan, self._cost_us(feats),
                            self._mem_bytes(feats, q), feats)

    @staticmethod
    def feasible_plans(q):
        # 列被索引覆盖时没有理由回表；不被覆盖时覆盖索引计划不可行
        if q.covering:
            return [FULL_SCAN, INDEX_COVERING]
        return [FULL_SCAN, INDEX_LOOKUP]

    def choose(self, q, stats):
        """选代价最小的计划；统计信息缺失时显式说明并保守选择。"""
        if stats is None:
            # 无法估算：绝不静默猜测。保守策略 = 避免回表随机 I/O 失控：
            # 能覆盖就走覆盖索引，否则走有界的顺序全表扫描。
            plan = INDEX_COVERING if q.covering else FULL_SCAN
            return Decision(plan, None,
                            "统计信息缺失，无法估算选择率/代价；"
                            "采用保守计划（%s），避免回表随机 I/O 风险" % plan)
        ests = {p: self.estimate(p, q, stats) for p in self.feasible_plans(q)}
        best = min(ests, key=lambda p: ests[p].est_us)
        return Decision(best, ests)


# ---------------------------------------------------------------- 执行引擎

class Engine:
    """行存堆 + 有序二级索引 + LRU 缓冲池的模拟引擎。

    实测耗时 = perf_counter 实测 CPU + 页故障 x 模拟磁盘延迟。
    页故障是执行时真实数出来的（顺序扫描每页一次故障；回表按缓冲池命中情况），
    代价模型并不知道这些真实值，只能估算 —— 估算与实测因此存在真实偏差。
    """

    def __init__(self, rows, row_width):
        self.rows = rows                       # [(key, v, pad), ...]
        self.row_width = row_width
        self.rpp = max(1, PAGE_SIZE // row_width)
        self.num_pages = math.ceil(len(rows) / self.rpp)
        entries = sorted((r[0], i) for i, r in enumerate(rows))
        self.idx_entries = entries
        self.idx_keys = [k for k, _ in entries]

    def build_stats(self):
        keys = self.idx_keys
        n = len(keys)
        nd = 1
        for i in range(1, n):
            if keys[i] != keys[i - 1]:
                nd += 1
        hist = Histogram(keys)
        # 聚簇度：采样前 K 个索引项，看触及多少不同堆页
        K = min(2000, n)
        pages = len({rid // self.rpp for _, rid in self.idx_entries[:K]})
        denom = K - max(1, K // self.rpp)
        cl = max(0.0, min(1.0, (K - pages) / denom)) if denom > 0 else 0.0
        return TableStats(n, self.row_width, nd, hist, cl)

    def run(self, plan, q):
        pool = OrderedDict()
        seq_pages = misses = 0
        index_rows = scan_rows = 0
        sort_nl = 0.0
        tracemalloc.start()
        t0 = time.perf_counter()
        if plan == FULL_SCAN:
            lo, hi = q.lo, q.hi
            res = [r for r in self.rows if lo <= r[0] <= hi]
            scan_rows = len(self.rows)
            seq_pages = self.num_pages
            if q.order_by_index:
                res.sort(key=lambda r: r[0])
                sort_nl = len(res) * math.log2(max(2, len(res)))
        elif plan == INDEX_COVERING:
            i0 = bisect.bisect_left(self.idx_keys, q.lo)
            i1 = bisect.bisect_right(self.idx_keys, q.hi)
            res = self.idx_keys[i0:i1]         # 覆盖：只取索引列，不回表
            index_rows = i1 - i0
        elif plan == INDEX_LOOKUP:
            i0 = bisect.bisect_left(self.idx_keys, q.lo)
            i1 = bisect.bisect_right(self.idx_keys, q.hi)
            ent = self.idx_entries[i0:i1]
            index_rows = i1 - i0
            rpp, rows = self.rpp, self.rows
            res = []
            ap = res.append
            for _k, rid in ent:                # 按索引序回表 -> 随机页访问
                pg = rid // rpp
                if pg in pool:
                    pool.move_to_end(pg)
                else:
                    misses += 1
                    pool[pg] = None
                    if len(pool) > POOL_PAGES:
                        pool.popitem(last=False)
                ap(tuple(rows[rid]))            # 物化投影行
        else:
            raise ValueError(plan)
        cpu_us = (time.perf_counter() - t0) * 1e6
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        io_us = seq_pages * SEQ_PAGE_US + misses * RAND_PAGE_US
        feats = dict(seq_pages=float(seq_pages), rand_pages=float(misses),
                     index_rows=float(index_rows), scan_rows=float(scan_rows),
                     sort_nlogn=sort_nl, out_rows=float(len(res)))
        return ExecResult(plan, cpu_us + io_us, cpu_us, float(peak),
                          feats, len(res))


# ---------------------------------------------------------------- 反馈修正

def calibrate(samples):
    """用 (实测特征, 实测耗时) 样本最小二乘重估代价常数（岭正则 + 高斯消元）。"""
    n = len(FEATURES)
    ata = [[0.0] * (n + 1) for _ in range(n)]
    for feats, actual in samples:
        x = [feats[f] for f in FEATURES]
        for i in range(n):
            xi = x[i]
            if xi == 0.0:
                continue
            row = ata[i]
            for j in range(n):
                row[j] += xi * x[j]
            row[n] += xi * actual
    for i in range(n):
        ata[i][i] += 1e-3                        # 岭正则，防奇异
    for col in range(n):                         # 高斯-若尔当消元
        piv = max(range(col, n), key=lambda r: abs(ata[r][col]))
        ata[col], ata[piv] = ata[piv], ata[col]
        d = ata[col][col]
        if abs(d) < 1e-12:
            continue
        for r in range(n):
            if r != col and ata[r][col] != 0.0:
                f = ata[r][col] / d
                for cc in range(col, n + 1):
                    ata[r][cc] -= f * ata[col][cc]
    sol = []
    for i in range(n):
        d = ata[i][i]
        sol.append(max(0.0, ata[i][n] / d) if abs(d) > 1e-12 else 0.0)
    return CostConstants(*sol)

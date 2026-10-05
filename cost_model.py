"""代价模型：覆盖索引 vs 回表的取舍判断（仅标准库）。

估算依据（不使用任何固定阈值，计划选择 = 候选计划估算代价取最小）：

1. 选择率 selectivity
   - 等值条件：sel = 1 / NDV（不同值个数，来自统计信息）
   - 范围条件：sel = 等深直方图按桶插值（桶内按值域均匀假设）
2. 行宽 row width
   - 每页行数 = 页大小 / 行宽 -> 堆页数 P = ceil(N / 每页行数)
   - 索引项宽 -> 索引页扇出 -> 索引叶页数
   - 行宽越大，同样行数下堆页越多，回表（随机页）代价越高
3. 排序要求
   - 计划输出行序若与 ORDER BY 一致（索引键有序）则免排序
   - 否则加收 k*log2(k) 次比较的 CPU 代价和 k*16B 的排序内存

回表堆页数估计（经典均匀假设公式，非阈值）：
   distinct_pages(k) = P * (1 - (1 - 1/P)^k)
   即 k 行命中行期望触及的不同堆页数；k 小时远小于 k，k 大时趋近 P，
   这就是"选择率升高后回表必然输给顺序全扫"的量化来源。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields as dc_fields

PAGE_SIZE = 8192
ROW_WIDTH = 128                      # 逻辑行宽（字节）：行头 + id(8) + k1(4) + k2(4) + payload(48) + 对齐
PAYLOAD_LEN = 48                     # payload 实际长度（字节）
INDEX_ENTRY_WIDTH = 16               # 索引项：k1(4) + k2(4) + rowid(8)
ROWS_PER_PAGE = PAGE_SIZE // ROW_WIDTH                 # 64 行/堆页
ENTRIES_PER_INDEX_PAGE = PAGE_SIZE // INDEX_ENTRY_WIDTH  # 512 项/索引页
assert ROWS_PER_PAGE == 64, "执行器用位移计算页号，要求每页 64 行"

COMPONENTS = ("seq_page", "rand_page", "cpu_row", "rand_row", "sort_cmp", "fixed")
MEM_COMPONENTS = ("sort_row", "cache_entry", "fixed_mem")


@dataclass
class CostConstants:
    """单位代价（毫秒）。初始值是通用经验猜测，允许被校准器按实测修正。"""
    seq_page_ms: float = 0.010    # 顺序读一页
    rand_page_ms: float = 0.050   # 随机读一页
    cpu_row_ms: float = 0.0005    # 每行 CPU（过滤 / 投影 / 随机行访问）
    rand_row_ms: float = 0.0010   # 回表时每行的堆行随机访问 CPU（高于顺序扫描行）
    sort_cmp_ms: float = 0.0002   # 每次排序比较
    fixed_ms: float = 0.05        # 每次查询固定开销
    # 内存分量（字节），初始为逻辑值，实测为 Python 实际对象开销，可校准
    sort_row_bytes: float = 16.0      # 排序缓冲每行（键+指针的逻辑值）
    cache_entry_bytes: float = 64.0   # 缓冲池每个缓存页条目的分摊开销
    fixed_mem_bytes: float = 8192.0   # 每次查询固定工作区

    def price(self, name: str) -> float:
        return getattr(self, name + "_ms")

    def unit(self, name: str) -> float:
        if name in COMPONENTS:
            return getattr(self, name + "_ms")
        return getattr(self, name + "_bytes")

    def scaled(self, name: str, factor: float) -> None:
        suffix = "_ms" if name in COMPONENTS else "_bytes"
        setattr(self, name + suffix, getattr(self, name + suffix) * factor)


@dataclass
class Stats:
    """ANALYZE 产物：行数、NDV、值域、等深直方图。"""
    n_rows: int
    ndv: int
    lo: int
    hi: int
    buckets: list  # 等深直方图边界，长度 B+1，每桶权重 1/B

    def selectivity_eq(self, v: int) -> float:
        if not (self.lo <= v <= self.hi):
            return 0.0
        return 1.0 / self.ndv

    def selectivity_range(self, lo: int, hi: int) -> float:
        if hi < self.lo or lo > self.hi or hi < lo:
            return 0.0
        lo = max(lo, self.lo)
        hi = min(hi, self.hi)
        nb = len(self.buckets) - 1
        sel = 0.0
        for i in range(nb):
            b_lo, b_hi = self.buckets[i], self.buckets[i + 1]
            overlap = min(hi, b_hi) - max(lo, b_lo)
            if overlap > 0:
                width = max(1, b_hi - b_lo)
                sel += (overlap / width) / nb
        return min(1.0, sel)


@dataclass
class Query:
    qid: str
    kind: str                # 'eq' | 'range'
    lo: int
    hi: int
    covered: bool            # 投影是否被索引 (k1, k2) 覆盖
    order_by: str | None     # None | 'k1' | 'k2'
    has_stats: bool          # 统计信息是否可用
    label: str


@dataclass
class Estimate:
    plan: str                # 'index_only' | 'lookup' | 'full_scan'
    feasible: bool           # False = 无法估算（统计信息缺失等）
    reason: str
    est_rows: float
    units: dict              # 各代价分量的"数量"（页数/行数/比较次数）
    total_ms: float
    mem_bytes: float
    mem_units: dict          # 内存分量数量：排序行数 / 缓存页数 / 固定工作区

    def components_ms(self, c: CostConstants) -> dict:
        return {k: v * c.price(k) for k, v in self.units.items()}


class CostModel:
    def __init__(self, n_rows: int, constants: CostConstants | None = None,
                 buffer_pages: int = 1024):
        self.n_rows = n_rows
        self.c = constants or CostConstants()
        self.buffer_pages = buffer_pages
        self.heap_pages = math.ceil(n_rows / ROWS_PER_PAGE)
        self.index_pages = math.ceil(n_rows / ENTRIES_PER_INDEX_PAGE)

    # ---- 选择率 -> 估计行数 ----
    def _est_rows(self, q: Query, stats: Stats) -> float:
        if q.kind == "eq":
            return stats.selectivity_eq(q.lo) * stats.n_rows
        return stats.selectivity_range(q.lo, q.hi) * stats.n_rows

    def _needs_sort(self, plan: str, q: Query) -> bool:
        if q.order_by is None:
            return False
        # 索引计划按 (k1) 有序输出；全表扫描无序
        return not (plan in ("index_only", "lookup") and q.order_by == "k1")

    def estimate_plan(self, plan: str, q: Query, stats: Stats) -> Estimate:
        k = self._est_rows(q, stats)
        P = self.heap_pages
        idx_pages = max(1, math.ceil(max(k, 1) / ENTRIES_PER_INDEX_PAGE))
        sort_rows = k if self._needs_sort(plan, q) else 0

        if plan == "index_only":
            units = {"seq_page": idx_pages, "cpu_row": k}
        elif plan == "lookup":
            heap = P * (1.0 - ((P - 1.0) / P) ** k) if k > 0 else 0.0
            units = {"seq_page": idx_pages, "rand_page": heap,
                     "cpu_row": k, "rand_row": k}
        elif plan == "full_scan":
            units = {"seq_page": P, "cpu_row": self.n_rows + k}
        else:
            raise ValueError(plan)

        units["sort_cmp"] = sort_rows * math.log2(max(sort_rows, 2))
        units["fixed"] = 1.0
        total = sum(u * self.c.price(n) for n, u in units.items())
        if plan == "lookup":
            cache_pages = min(units["rand_page"], self.buffer_pages)
        elif plan == "full_scan":
            cache_pages = min(P, self.buffer_pages)
        else:
            cache_pages = 0
        mem_units = {"sort_row": sort_rows, "cache_entry": cache_pages, "fixed_mem": 1.0}
        mem = sum(u * self.c.unit(n) for n, u in mem_units.items())
        return Estimate(plan, True, "", k, units, total, mem, mem_units)

    def choose(self, q: Query, stats: Stats | None) -> tuple[Estimate, list[Estimate]]:
        """返回 (选中的估算, 全部候选)。统计缺失时显式给出保守选择。"""
        if stats is None or not q.has_stats:
            # 无法估算选择率 -> 无法比较代价。保守原则：避免无界随机 I/O。
            # 覆盖索引扫描代价有界（≤ 整个索引），否则退化为有界的顺序全扫，
            # 绝不选择最坏情况为 N 次随机页读的回表计划。
            plan = "index_only" if q.covered else "full_scan"
            reason = ("统计信息缺失，无法估算选择率；保守选择 "
                      + ("覆盖索引扫描（代价有界，≤ 整个索引）" if plan == "index_only"
                         else "顺序全表扫描（避免最坏 N 次随机回表）"))
            return Estimate(plan, False, reason, float("nan"), {}, float("nan"),
                            float("nan"), {}), []

        candidates = (["index_only"] if q.covered else []) + ["lookup", "full_scan"]
        estimates = [self.estimate_plan(p, q, stats) for p in candidates]
        best = min(estimates, key=lambda e: e.total_ms)
        return best, estimates


def _calibrate(constants: CostConstants, records: list[tuple[dict, float]],
               names: tuple, rounds: int, damping: float) -> CostConstants:
    """按实测耗时修正单位代价。

    records: [(units, actual_ms)]。每轮对每个分量计算：
        加权误差比 = Σ(分量占比 * 实际/估计) / Σ(分量占比)
    以 damping 阻尼的几何方式乘到对应单位代价上，并限制单轮调整幅度，
    避免个别噪声查询把常数带飞。
    """
    c = CostConstants(**{f.name: getattr(constants, f.name)
                         for f in dc_fields(CostConstants)})
    for _ in range(rounds):
        for name in names:
            w_sum = r_sum = 0.0
            for units, actual in records:
                est = sum(u * c.unit(n) for n, u in units.items())
                if est <= 1e-9 or actual <= 0:
                    continue
                w = units.get(name, 0.0) * c.unit(name) / est
                w_sum += w
                r_sum += w * (actual / est)
            if w_sum > 0:
                factor = (r_sum / w_sum) ** damping
                c.scaled(name, min(4.0, max(0.25, factor)))
    return c


def calibrate(constants: CostConstants, records: list[tuple[dict, float]],
              rounds: int = 3, damping: float = 0.7) -> CostConstants:
    """按实测耗时修正时间单位代价（按分量占比加权的阻尼几何修正）。"""
    return _calibrate(constants, records, COMPONENTS, rounds, damping)


def calibrate_mem(constants: CostConstants, records: list[tuple[dict, float]],
                  rounds: int = 3, damping: float = 0.7) -> CostConstants:
    """按 tracemalloc 实测修正内存单位（排序行/缓存条目的实际对象开销）。"""
    return _calibrate(constants, records, MEM_COMPONENTS, rounds, damping)

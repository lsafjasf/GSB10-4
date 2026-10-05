"""模拟存储引擎：堆文件（按页组织）+ 有序索引 (k1, k2, rowid)。

- 堆：array 存储，行 r 位于第 r // ROWS_PER_PAGE 页，模拟页式布局。
- 索引：按 k1 排序的三条并行数组（k1, k2, rowid），范围定位用二分。
- 执行器实现三种计划，统计顺序/随机页读，输出流式汇总（count+checksum），
  排序等内部缓冲真实分配，供 tracemalloc 测量内存。
"""
from __future__ import annotations

import bisect
import math
import random
from array import array
from dataclasses import dataclass

from cost_model import (ENTRIES_PER_INDEX_PAGE, PAYLOAD_LEN, ROWS_PER_PAGE,
                        Query, Stats)

PAGE_SHIFT = 6  # log2(ROWS_PER_PAGE=64)


@dataclass
class ExecResult:
    rows: int
    seq_pages: int
    rand_pages: int
    sort_rows: int
    checksum: int


class Table:
    def __init__(self, n: int, ndv: int, seed: int = 42):
        rng = random.Random(seed)
        self.n = n
        self.ndv = ndv
        self.ids = array("q", range(n))
        self.k1 = array("i", [rng.randrange(ndv) for _ in range(n)])
        self.k2 = array("i", [rng.randrange(1000) for _ in range(n)])
        self.payload = (b"x" * PAYLOAD_LEN) * n  # 定长 payload 区
        # 建索引：按 k1 排序的 (k1, k2, rowid)
        order = sorted(range(n), key=self.k1.__getitem__)
        self.idx_k1 = array("i", [self.k1[i] for i in order])
        self.idx_k2 = array("i", [self.k2[i] for i in order])
        self.idx_rid = array("i", order)
        self.heap_pages = math.ceil(n / ROWS_PER_PAGE)

    def analyze(self, n_buckets: int = 256) -> Stats:
        """收集统计信息：NDV、值域、等深直方图。"""
        idx = self.idx_k1
        buckets = [idx[min(self.n - 1, (i * self.n) // n_buckets)]
                   for i in range(n_buckets)]
        buckets.append(idx[-1])
        return Stats(n_rows=self.n, ndv=len(set(self.k1)),
                     lo=idx[0], hi=idx[-1], buckets=buckets)

    def actual_count(self, q: Query) -> int:
        """真实命中行数（用索引二分求得，作为估算的对照真值）。"""
        lo = bisect.bisect_left(self.idx_k1, q.lo)
        hi = bisect.bisect_right(self.idx_k1, q.hi)
        return hi - lo


class Executor:
    def __init__(self, table: Table, buffer_pages: int = 1024):
        self.t = table
        self.buffer_pages = buffer_pages
        self._cache: dict[int, bool] = {}

    def _touch(self, page: int, kind: str, res: ExecResult) -> None:
        if page in self._cache:
            return
        if len(self._cache) >= self.buffer_pages:
            self._cache.pop(next(iter(self._cache)))  # FIFO 淘汰
        self._cache[page] = True
        if kind == "rand":
            res.rand_pages += 1
        else:
            res.seq_pages += 1

    def execute(self, plan: str, q: Query) -> ExecResult:
        # 每次执行使用冷缓冲池，保证各查询测量可比
        self._cache = {}
        res = ExecResult(0, 0, 0, 0, 0)
        t = self.t
        i0 = bisect.bisect_left(t.idx_k1, q.lo)
        i1 = bisect.bisect_right(t.idx_k1, q.hi)
        cnt = i1 - i0

        if plan == "index_only":
            res.seq_pages += max(1, math.ceil(max(cnt, 1) / ENTRIES_PER_INDEX_PAGE))
            s = 0
            for i in range(i0, i1):
                s += t.idx_k2[i]
            res.rows, res.checksum = cnt, s
            if q.order_by == "k2":
                buf = [t.idx_k2[i] for i in range(i0, i1)]
                buf.sort()
                res.sort_rows = cnt
                res.checksum += buf[0] if buf else 0

        elif plan == "lookup":
            res.seq_pages += max(1, math.ceil(max(cnt, 1) / ENTRIES_PER_INDEX_PAGE))
            s = 0
            for i in range(i0, i1):
                r = t.idx_rid[i]
                self._touch(r >> PAGE_SHIFT, "rand", res)
                s += t.ids[r] + t.k2[r] + t.payload[r * PAYLOAD_LEN]
            res.rows, res.checksum = cnt, s
            if q.order_by == "k2":
                buf = sorted(t.k2[t.idx_rid[i]] for i in range(i0, i1))
                res.sort_rows = cnt
                res.checksum += buf[0] if buf else 0

        elif plan == "full_scan":
            for p in range(t.heap_pages):
                self._touch(p, "seq", res)
            s = cnt2 = 0
            lo, hi, k1, ids, k2, pl = q.lo, q.hi, t.k1, t.ids, t.k2, t.payload
            for i in range(t.n):
                if lo <= k1[i] <= hi:
                    cnt2 += 1
                    s += ids[i] + k2[i] + pl[i * PAYLOAD_LEN]
            res.rows, res.checksum = cnt2, s
            if q.order_by is not None:
                key = k1 if q.order_by == "k1" else k2
                buf = sorted(key[i] for i in range(t.n) if lo <= k1[i] <= hi)
                res.sort_rows = cnt2
                res.checksum += buf[0] if buf else 0
        else:
            raise ValueError(plan)
        return res

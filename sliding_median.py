"""滑动窗口中位数：双堆（大顶堆 lo + 小顶堆 hi）+ 延迟删除。

只依赖标准库（heapq / collections）。
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from typing import Iterable, List, Sequence, Union

Number = Union[int, float]
Median = float


class SlidingMedian:
    """定长滑动窗口上的中位数增量维护器。

    结构
    ----
    * ``lo``：大顶堆（用存相反数的小顶堆模拟），保存窗口中较小的一半。
    * ``hi``：小顶堆，保存窗口中较大的一半。
    * ``delayed``：延迟删除计数。元素出窗口时先记账，等它爬到堆顶再真正 pop，
      因此“大量重复值”不会影响删除（删除的是堆顶的那个值，计数一一抵消）。
    * 定期压缩：埋在堆中间的待删元素不会自己浮到堆顶，若不清理会随 n 线性
      堆积。每当“待删元素数 > 存活元素数”时重建两个堆、丢弃全部待删元素，
      均摊 O(1)，保证堆长始终 O(k)。
    * ``lo_size`` / ``hi_size``：两个堆“当前有效元素”的个数（不含待删项）。

    平衡规则
    --------
    每次 add / remove 后调用 ``_rebalance``，保持：

        lo_size == hi_size        （窗口大小为偶数）
        lo_size == hi_size + 1    （窗口大小为奇数）

    * ``lo_size > hi_size + 1``：把 lo 堆顶挪到 hi。
    * ``lo_size < hi_size``：把 hi 堆顶挪到 lo。
    * 允许的合法状态只有上面两种，因此 lo 永远不比 hi 少元素。

    中位数取值约定
    --------------
    * 窗口大小为奇数：中位数 = lo 堆顶（即第 (k+1)/2 小的元素）。
    * 窗口大小为偶数：中位数 = (lo 堆顶 + hi 堆顶) / 2
      （即第 k/2 小与第 k/2+1 小的平均值，结果类型为 float）。

    复杂度
    ------
    每个元素入堆一次、最多出堆一次；压缩在待删数翻倍时才触发，均摊 O(1)。
    单窗口均摊 O(log k)，总长 n 为 O(n log k)，额外空间 O(k)。
    """

    def __init__(self, k: int) -> None:
        if k < 1:
            raise ValueError("window size k must be >= 1")
        self.k = k
        self.lo: List[Number] = []   # 大顶堆，元素取负存入
        self.hi: List[Number] = []   # 小顶堆
        self.delayed: "defaultdict[Number, int]" = defaultdict(int)
        self.lo_size = 0
        self.hi_size = 0

    # ---- 内部维护 -------------------------------------------------------

    def _prune(self, heap: List[Number]) -> None:
        """把指定堆顶部所有“待删除”的元素真正弹出。"""
        is_lo = heap is self.lo
        while heap:
            top = -heap[0] if is_lo else heap[0]
            if self.delayed.get(top, 0) == 0:
                return
            self.delayed[top] -= 1
            if self.delayed[top] == 0:
                del self.delayed[top]
            heapq.heappop(heap)

    def _compact(self) -> None:
        """待删元素多于存活元素时，重建两个堆并丢弃全部待删元素。

        每个待删元素在产生时已付出 O(1) 记账代价，重建耗时 O(堆长) 由这些
        代价均摊，因此每次 remove 均摊仍是 O(log k)。
        """
        live = self.lo_size + self.hi_size
        if len(self.lo) + len(self.hi) - live <= live:
            return

        def keep_live(heap: List[Number], sign: int) -> List[Number]:
            out: List[Number] = []
            for item in heap:
                v = sign * item
                if self.delayed.get(v, 0):
                    self.delayed[v] -= 1
                    if self.delayed[v] == 0:
                        del self.delayed[v]
                else:
                    out.append(item)
            heapq.heapify(out)
            return out

        self.lo = keep_live(self.lo, -1)
        self.hi = keep_live(self.hi, +1)

    def _rebalance(self) -> None:
        if self.lo_size > self.hi_size + 1:
            heapq.heappush(self.hi, -heapq.heappop(self.lo))
            self.lo_size -= 1
            self.hi_size += 1
            self._prune(self.lo)
        elif self.lo_size < self.hi_size:
            heapq.heappush(self.lo, -heapq.heappop(self.hi))
            self.hi_size -= 1
            self.lo_size += 1
            self._prune(self.hi)

    # ---- 对外操作 -------------------------------------------------------

    def add(self, x: Number) -> None:
        """元素 x 进入窗口。"""
        if self.lo_size == 0 or x <= -self.lo[0]:
            heapq.heappush(self.lo, -x)
            self.lo_size += 1
        else:
            heapq.heappush(self.hi, x)
            self.hi_size += 1
        self._rebalance()

    def remove(self, x: Number) -> None:
        """元素 x 离开窗口（延迟删除）。

        用 x 与 lo 堆顶比较来决定它记在哪一半：重复值记在哪一半都等价，
        因为只关心值的多重集合和两侧有效计数。
        """
        if self.lo and x <= -self.lo[0]:
            self.lo_size -= 1
            if x == -self.lo[0]:
                heapq.heappop(self.lo)
            else:
                self.delayed[x] += 1
            self._prune(self.lo)
        else:
            self.hi_size -= 1
            if self.hi and x == self.hi[0]:
                heapq.heappop(self.hi)
            else:
                self.delayed[x] += 1
            self._prune(self.hi)
        self._rebalance()
        self._compact()

    def median(self) -> Median:
        """当前窗口的中位数（调用前窗口必须非空）。"""
        if self.lo_size == 0:
            raise RuntimeError("window is empty")
        if self.lo_size > self.hi_size:
            return float(-self.lo[0])
        return (-self.lo[0] + self.hi[0]) / 2.0

    @property
    def size(self) -> int:
        return self.lo_size + self.hi_size


def sliding_median(nums: Sequence[Number], k: int) -> List[Median]:
    """返回 nums 上所有长度为 k 的滑动窗口中位数，共 len(nums) - k + 1 个。"""
    if k < 1 or k > len(nums):
        raise ValueError("require 1 <= k <= len(nums)")
    sm = SlidingMedian(k)
    result: List[Median] = []
    for i, x in enumerate(nums):
        sm.add(x)
        if i >= k:
            sm.remove(nums[i - k])
        if i >= k - 1:
            result.append(sm.median())
    return result

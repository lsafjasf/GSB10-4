"""动态表（Dynamic Table）核心状态。

语义对齐 QPACK (RFC 9204) / HPACK (RFC 7541) 的动态表：
  * 每条表项大小 = len(name) + len(value) + 32（32 字节协议开销）。
  * 表有当前容量 capacity，且 capacity 永远不得超过建表时协商的
    最大容量 max_capacity（等价于 HTTP/2 SETTINGS_HEADER_TABLE_SIZE）。
  * 插入新表项时，若空间不足，则从表的最前端（最旧表项）开始逐条驱逐，
    直到能放下新表项；这是严格的 FIFO 驱逐。
  * 若新表项自身大小超过当前容量，该插入指令无法执行，抛出
    EntryTooLarge，绝不插入半截状态。
  * 表项同时支持两种索引：
      - 绝对索引 absolute：从该表历史上第 0 条插入开始编号，被驱逐的
        表项占用较小的绝对索引；
      - 相对索引 relative：最新表项为 0，向更旧的方向递增（QPACK
        头部块里用的就是这种，配合 Base 使用）。
  * dropped 记录已驱逐表项条数；absolute = dropped + 表内位置。
本模块只维护状态与规则，不负责线路编码，便于独立对拍与测试。
"""

from collections import deque

ENTRY_OVERHEAD = 32


def entry_size(name: str, value: str) -> int:
    return len(name.encode("utf-8")) + len(value.encode("utf-8")) + ENTRY_OVERHEAD


class DynamicTableError(Exception):
    """动态表相关错误的基类。"""


class CapacityExceeded(DynamicTableError):
    """请求的容量超过协商上限 max_capacity。"""


class EntryTooLarge(DynamicTableError):
    """表项自身大于当前容量，插入指令无法执行。"""


class IndexOutOfBounds(DynamicTableError):
    """索引指向不存在的表项（已驱逐、尚未插入或负数）。"""


class DynamicTable:
    def __init__(self, max_capacity: int):
        if max_capacity < 0:
            raise ValueError("max_capacity 必须非负")
        self._max_capacity = max_capacity
        self._capacity = 0
        self._size = 0
        # entries: 队首最旧、队尾最新；deque[i] 即表内位置 i。
        self._entries = deque()
        self._dropped = 0

    @property
    def max_capacity(self) -> int:
        return self._max_capacity

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def size(self) -> int:
        return self._size

    @property
    def dropped(self) -> int:
        """已被驱逐的表项总数（也是当前最旧表项的绝对索引）。"""
        return self._dropped

    @property
    def count(self) -> int:
        """当前表内表项条数。"""
        return len(self._entries)

    @property
    def insert_count(self) -> int:
        """历史插入总数 = 已驱逐数 + 当前条数，即下一条新表项的绝对索引。"""
        return self._dropped + len(self._entries)

    def snapshot(self):
        """返回当前表内容快照，按“最新 -> 最旧”（相对索引 0 在前）。"""
        return [
            (rel, self._dropped + len(self._entries) - 1 - rel, name, value)
            for rel, (name, value) in enumerate(reversed(self._entries))
        ]

    def set_capacity(self, capacity: int) -> None:
        """Set Dynamic Table Capacity：缩小容量会立即驱逐最旧表项。"""
        if capacity < 0 or capacity > self._max_capacity:
            raise CapacityExceeded(
                f"容量 {capacity} 超出协商上限 {self._max_capacity}，指令无法执行")
        self._capacity = capacity
        self._evict_until_fit(0)

    def _evict_until_fit(self, incoming_size: int) -> None:
        while self._entries and self._size + incoming_size > self._capacity:
            name, value = self._entries.popleft()
            self._size -= entry_size(name, value)
            self._dropped += 1

    def insert(self, name: str, value: str) -> int:
        """插入表项，返回该表项的绝对索引；必要时 FIFO 驱逐旧表项。"""
        size = entry_size(name, value)
        if size > self._capacity:
            raise EntryTooLarge(
                f"表项大小 {size} > 当前容量 {self._capacity}，插入指令无法执行")
        self._evict_until_fit(size)
        self._entries.append((name, value))
        self._size += size
        return self.insert_count - 1

    def get_absolute(self, index: int):
        """按绝对索引取表项；越界（已驱逐/未插入/负数）报 IndexOutOfBounds。"""
        if index < self._dropped or index >= self.insert_count:
            raise IndexOutOfBounds(
                f"绝对索引 {index} 越界（有效区间 "
                f"[{self._dropped}, {self.insert_count})，表为空）"
                if self.insert_count == self._dropped else
                f"绝对索引 {index} 越界（有效区间 [{self._dropped}, {self.insert_count})）")
        return self._entries[index - self._dropped]

    def get_relative(self, rel: int):
        """按相对索引取表项：0 = 最新，越大越旧。"""
        n = len(self._entries)
        if rel < 0 or rel >= n:
            raise IndexOutOfBounds(
                f"相对索引 {rel} 越界（当前表内 {n} 条，有效区间 [0, {n}))")
        return self._entries[n - 1 - rel]

    def find(self, name: str, value: str):
        """在表中查找 name/value 完全匹配项，返回相对索引（最新为 0），找不到返回 None。"""
        for rel, entry in enumerate(reversed(self._entries)):
            if entry == (name, value):
                return rel
        return None

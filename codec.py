"""头部压缩编解码层（QPACK 风格，标准库实现）。

在动态表之上实现两端同步与异常检出：

1) 指令流（encoder stream）
   编码端产生两类带严格递增序号 seq 的指令：
     ("capacity", new_capacity)   设置动态表容量
     ("insert", name, value)      插入一条表项
   解码端按 seq 顺序应用。期望的下一条序号记为 expected_seq：
     - 收到的 seq < expected_seq：重复/乱序，报 UpdateReordered；
     - 收到的 seq > expected_seq：中间有指令丢失，报 UpdateLost，
       并可从缺口序号直接定位丢了哪几条。

2) 头部块前缀（header block prefix）
   每个头部块附带：
     required_insert_count (RIC)：解码该块所需的最小插入计数
                                  （= 块中引用到的最大绝对索引 + 1）；
     base：编码时 insert_count，相对索引据此换算成绝对索引
            absolute = base - 1 - relative（QPACK 规则）。
   解码前若 RIC > 解码端 insert_count，说明引用的表项还没收到
   （更新指令丢失或乱序），报 MissingReference，拒绝解码——
   绝不会拿错误表项解出错误头部。

3) 确认流（decoder stream）
   解码端成功处理一个头部块后发 SectionAcknowledgment，并上报
   Known Received Count（= 当前 insert_count）。编码端据此知道
   哪些旧表项已被对端收到，才可安全驱逐（QPACK 的不可驱逐规则）。
   本实现保守起见：绝对索引 >= known_received 的表项一律不驱逐；
   若插入需要驱逐这些表项，则抛 UnsafeEviction，指令不执行。

说明：为突出状态机与检出逻辑，指令与头部块用 Python 元组表达，
不做实际字节整型编码；语义（计数、索引、驱逐、检出）与协议一致。
"""

from dataclasses import dataclass, field
from typing import List, Tuple

from dynamic_table import (
    DynamicTable,
    DynamicTableError,
    CapacityExceeded,
    EntryTooLarge,
    IndexOutOfBounds,
)

Header = Tuple[str, str]
Instruction = Tuple  # ("capacity", seq, cap) / ("insert", seq, name, value)
HeaderBlock = Tuple[int, int, List[Tuple]]  # (RIC, base, fields)


class CodecError(DynamicTableError):
    pass


class UpdateLost(CodecError):
    """指令序号出现缺口：有更新指令在传输中丢失。"""

    def __init__(self, expected, got):
        self.expected = expected
        self.got = got
        super().__init__(
            f"更新指令丢失：期望序号 {expected}，实际收到 {got}，"
            f"缺失序号 {list(range(expected, got))}")


class UpdateReordered(CodecError):
    """收到的指令序号小于期望值：重复或乱序到达。"""

    def __init__(self, expected, got):
        self.expected = expected
        self.got = got
        super().__init__(f"更新指令乱序/重复：期望序号 {expected}，实际收到 {got}")


class MissingReference(CodecError):
    """头部块引用了解码端尚不存在的表项（更新丢失或乱序）。"""

    def __init__(self, ric, have):
        self.ric = ric
        self.have = have
        super().__init__(
            f"引用缺失：头部块要求插入计数 {ric}，解码端当前仅 {have}，"
            f"拒绝解码（可能有 {ric - have} 条更新丢失/未到）")


class BadBlockPrefix(CodecError):
    pass


class UnsafeEviction(CodecError):
    """驱逐会移除对端尚未确认收到的表项，指令不能执行。"""


@dataclass
class Encoder:
    max_capacity: int
    table: DynamicTable = field(init=False)
    _seq: int = field(default=0, init=False)
    known_received: int = field(default=0, init=False)

    def __post_init__(self):
        self.table = DynamicTable(self.max_capacity)

    def set_capacity(self, capacity: int) -> Instruction:
        self._check_eviction_safety(capacity)
        self.table.set_capacity(capacity)  # 超上限直接抛 CapacityExceeded
        ins = ("capacity", self._seq, capacity)
        self._seq += 1
        return ins

    def _check_eviction_safety(self, capacity: int, incoming_size: int = 0) -> None:
        """模拟驱逐：若需要驱逐对端尚未确认收到的表项，抛 UnsafeEviction。"""
        from dynamic_table import entry_size
        # 表项比整个容量还大：再怎么驱逐也放不下，交由 DynamicTable 报 EntryTooLarge
        if incoming_size > capacity:
            return
        simulated_dropped = self.table.dropped
        simulated_size = self.table.size
        n = self.table.count
        while n > 0 and simulated_size + incoming_size > capacity:
            if simulated_dropped >= self.known_received:
                raise UnsafeEviction(
                    f"需要驱逐未确认表项（绝对索引 {simulated_dropped}，"
                    f"对端仅确认到 {self.known_received}），指令不能执行")
            oldest = self.table.get_absolute(simulated_dropped)
            simulated_size -= entry_size(*oldest)
            simulated_dropped += 1
            n -= 1

    def insert(self, name: str, value: str) -> Instruction:
        from dynamic_table import entry_size
        self._check_eviction_safety(self.table.capacity, entry_size(name, value))
        self.table.insert(name, value)
        ins = ("insert", self._seq, name, value)
        self._seq += 1
        return ins

    def encode(self, headers: List[Header]) -> HeaderBlock:
        base = self.table.insert_count
        fields = []
        max_ref = -1
        for name, value in headers:
            rel = self.table.find(name, value)
            if rel is not None:
                abs_index = base - 1 - rel
                fields.append(("indexed", rel))
                max_ref = max(max_ref, abs_index)
            else:
                fields.append(("literal", name, value))
        ric = max_ref + 1
        return (ric, base, fields)

    def on_ack(self, received_count: int) -> None:
        """处理解码端 Section Acknowledgment + Known Received Count。"""
        if received_count < self.known_received:
            raise CodecError("Known Received Count 不应倒退")
        self.known_received = max(self.known_received, received_count)


@dataclass
class Decoder:
    max_capacity: int
    table: DynamicTable = field(init=False)
    expected_seq: int = field(default=0, init=False)
    last_block_index: int = field(default=-1, init=False)

    def __post_init__(self):
        self.table = DynamicTable(self.max_capacity)

    def apply(self, ins: Instruction) -> None:
        """应用一条编码端指令；序号缺口/乱序在此被检出。"""
        op = ins[0]
        seq = ins[1]
        if seq > self.expected_seq:
            raise UpdateLost(self.expected_seq, seq)
        if seq < self.expected_seq:
            raise UpdateReordered(self.expected_seq, seq)
        self.expected_seq += 1
        if op == "capacity":
            self.table.set_capacity(ins[2])
        elif op == "insert":
            self.table.insert(ins[2], ins[3])
        else:
            raise CodecError(f"未知指令类型 {op!r}")

    def decode(self, block: HeaderBlock) -> Tuple[List[Header], int]:
        """解码头部块；引用缺失时抛 MissingReference，不产出任何头部。"""
        ric, base, fields = block
        if ric < 0 or base < ric:
            raise BadBlockPrefix(f"非法块前缀 RIC={ric}, base={base}")
        if ric > self.table.insert_count:
            raise MissingReference(ric, self.table.insert_count)
        headers: List[Header] = []
        for field_ in fields:
            kind = field_[0]
            if kind == "indexed":
                rel = field_[1]
                abs_index = base - 1 - rel
                name, value = self.table.get_absolute(abs_index)
            elif kind == "literal":
                name, value = field_[1], field_[2]
            else:
                raise CodecError(f"未知字段类型 {kind!r}")
            headers.append((name, value))
        return headers, self.table.insert_count

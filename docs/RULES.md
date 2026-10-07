# 动态表规则说明

本实现的动态表语义与 **RFC 7541 §4（HPACK 动态表）** 一致；头部块前缀与
更新流的编号机制参照 **RFC 9204（QPACK）§4.4/§4.5.1.1**。仅使用 Python 3
标准库。

## 1. 条目大小

- 每个条目的大小为 `len(name) + len(value) + 32`（32 字节是每条目固定
  开销，长度按 UTF-8 字节计）。
- 表大小 = 所有驻留条目大小之和。
- 两个容量概念：
  - `max_capacity`：协议上限（对端 SETTINGS 给出的硬上限），建表时给定，
    不可超过；
  - `capacity`：当前动态容量，编码器可用“容量更新指令”在
    `[0, max_capacity]` 内任意调整。

## 2. 插入规则（RFC 7541 §4.4）

1. 新条目一律插在**表头**（最新在前，相对索引 0 指向它）。
2. 插入前从**表尾**（最旧）开始逐出条目，直到 `表大小 + 新条目大小
   ≤ capacity` 为止；只逐出必要数量。
3. 若新条目自身大小 `> capacity`：清空整张表，**该条目不加入**
   （插入返回 `False`，`insert_count` 不增加）。
4. 每次成功插入后校验不变量，不满足即抛 `TableInvariantViolation`：
   - `size ≤ capacity ≤ max_capacity`；
   - `驻留条目数 == insert_count − dropped_count`。

## 3. 容量更新规则

- 更新到 `[0, max_capacity]`：立即生效，并按“从最旧开始”的顺序逐出
  直到满足新容量。
- 更新到 `> max_capacity`（或负值）：**拒绝执行**，抛
  `CapacityLimitExceeded`，表内容保持不变。
- 解码端收到越界容量指令同样拒绝，不会进入错误状态。

## 4. 索引规则

- **绝对索引**：第一个插入的条目为 0，单调递增；即使条目被逐出，
  绝对索引也不复用。
- **相对索引（pre-base）**：相对 Base 编址，`abs = Base − 1 − rel`。
- **post-base 索引**：`abs = Base + i`（解码器支持）。
- 查找结果分三类：
  - `abs < 0` 或 `abs ≥ insert_count` → `IndexOutOfRange`；
  - `abs < dropped_count`（已被逐出）→ `EvictedEntryReference`
    （`IndexOutOfRange` 的子类）；
  - 正常命中。

## 5. 两端状态同步与逐操作对拍

- 所有表变更（插入、容量更新）只允许编码器发出，并通过**编码器更新流**
  按字节顺序重放给解码器；解码端用同一份 `DynamicTable` 规则执行。
- 更新流按 **chunk** 分帧：`[u32 seq][u32 payload_len][指令…]`，
  `seq` 从 0 开始单调递增。
- 指令：
  - `0x01` 插入：`name(u16长度前缀字符串) + value`；
  - `0x02` 容量更新：`u32 新容量`。
- 每个 chunk 应用后，两端的完整快照（capacity/size/insert_count/
  dropped_count/含绝对索引的全部条目）必须逐项相等。
  `demo_trace.py` 对每一步做对拍并写入 `docs/trace_step_by_step.txt`。

## 6. 丢失 / 乱序更新的解码层检出

两层独立防线，任何一层不过都**直接报错，绝不解出错误头部**：

1. **chunk 序号检查**（更新流层）
   - 收到的 `seq > expected`：中间丢包，抛 `LostUpdateError`，
         消息含缺失数量；
   - 收到的 `seq < expected`：乱序/重复投递，抛
     `OutOfOrderUpdateError`。
2. **Required Insert Count 检查**（头部块层，RFC 9204 §4.5.1.1）
   - 每个头部块前缀为
     `[u32 EncodedInsertCount][u8 BaseSign][u32 DeltaBase]`；
   - 解码器按 RFC 公式把回绕的 EIC 还原成 RIC，与其
     `KnownReceivedCount` 比较：`RIC > KRC` 说明块依赖的更新还没到
     （丢失或乱序），抛 `BlockedHeaderBlock`；
   - `Base > insert_count` 同样判为阻塞；非法回绕/负 Base 抛
     `InvalidHeaderBlock`。

## 7. 头部块字段格式

前缀之后是零个或多个字段：

- `0x01` 相对索引引用：`u32 rel`；
- `0x02` post-base 引用：`u32 i`；
- `0x03` 字面量：`name 字符串 + value 字符串`；
- `0x04` 仅名引用字面量：`u32 rel + value 字符串`。

字符串 = `u16 字节长度 + UTF-8 数据`。

## 8. 错误类型汇总

| 异常 | 触发条件 |
|---|---|
| `CapacityLimitExceeded` | 容量更新超过协议上限 |
| `TableInvariantViolation` | size/容量/计数不变量被破坏（表溢出） |
| `IndexOutOfRange` | 索引超出 `[0, insert_count)` |
| `EvictedEntryReference` | 引用已被逐出的条目 |
| `InvalidInstruction` | 更新指令无法执行（如插入超大条目、未知 tag、长度不符） |
| `LostUpdateError` | 更新 chunk 序号出现缺口 |
| `OutOfOrderUpdateError` | 更新 chunk 序号倒退/重复 |
| `BlockedHeaderBlock` | 头部块依赖的表更新尚未到达（RIC/Base 超前） |
| `InvalidHeaderBlock` | 头部块前缀/字段非法 |
| `TruncatedData` | 报文被截断 |

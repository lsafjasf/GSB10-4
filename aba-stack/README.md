# 无锁栈的 ABA 防护（Node.js 标准库）

Treiber 无锁栈，基于 `SharedArrayBuffer` + `Atomics.compareExchange`（真实硬件 CAS，
64 位标签指针一次原子比较交换），`worker_threads` 提供真并行。仅用 Node.js 标准库。

## 防护方案：版本号（标签指针）+ 延迟回收（EBR），两者结合

1. **标签指针（版本号）**：栈顶指针与空闲链表头都是 64 位字
   `(tag << 32) | (nodeIndex + 1)`。每次 CAS 成功都把 `tag` 加 1（模 `2^tagBits`）。
   即使指针部分回到旧值 A，只要 tag 不同，CAS 就不会误判成功。
2. **延迟回收（3-epoch EBR）**：`pop` 摘除的节点不立即回池，先挂入本线程
   `retired[epoch]` 袋。全局 epoch 只有在**所有活跃线程都声明处于当前 epoch**
   时才前进；退役于 epoch `r` 的节点，只有当全局 epoch 前进两轮（到 `(r+2) mod 3`）
   且复查无任何活跃线程声明在待回收 epoch 时，才真正放回空闲链表（唯一释放点，
   见 `stack.js` 的 `scan()` / `freeNode()`）。
3. **结合的理由**：EBR 保证"任何线程临界区内，同一 `(节点, tag)` 组合不会复现"
   （节点复用是 `(ptr, tag)` 重合的必要条件，而复用被 EBR 推迟到所有线程越过
   静止点之后），因此 tag 只需极少位；标签指针则让每次 CAS 失败即重试，
   无需依赖回收器保证读到的 `next` 有意义。

## 版本号回绕怎么处理

tag 按模 `2^tagBits` 回绕，**不做特殊处理，靠 EBR 使回绕无害**：回绕要造成误判，
需要同一节点带着同一 tag 在某线程的临界区窗口内重新成为栈顶，而节点重新入栈
必须先被回收，回收又被 EBR 阻塞到该线程离开临界区之后——矛盾，故不可能。
测试 T5 把 `tagBits` 压到 **2**（每 4 次栈顶更新就回绕一次），4P4C 跑 100 万元素
依然守恒，即回绕路径被反复命中也无误。32 位 tag 下回绕需 2^32 次更新，实际不可达。

## 回收时机论证

- **何时可以真正释放**：节点在 `pop` 的 CAS 成功时只是*逻辑删除*。只有当
  所有线程都越过静止点（没有任何线程仍可能持有该节点地址）时才可回池复用。
  EBR 判定：退役于 epoch `r`，当全局 epoch 到 `(r+2) mod 3` 且无活跃线程声明在
  `r` 时，任何活跃线程都是在节点不可达之后才进入临界区的，不可能持有其引用。
- **提前释放为何出错**：线程 T 在 `pop` 中读到 `head=A`、`A.next=B` 后被切换走；
  若 A 被立即回池，A 会被重新分配（值和 `next` 被改写）并可能重新压回栈顶。
  T 醒来执行 `CAS(head, A, B)`：无版本号时指针相等即误判成功，把栈顶改回已出栈的
  B——丢失元素、同一节点被多条链引用（重复弹出）、甚至成环；有版本号但回绕后
  `(A, tag)` 完全重合时同理。空闲链表头同理会被 ABA 打穿（节点从空闲链丢失/
  重复分配）。
- **实证（T6 对照实验）**：`ebr=false`（摘除即回池）+ `tagBits=2` + 在 CAS 前
  人为自旋放宽竞态窗口，4P4C 40 万元素实测：

  ```
  pushed=400000 popped=400003 remaining=401024(守卫截断,链表已成环)
  dup=687943 lost=286916 -> CONSERVATION VIOLATED
  ```

  同一代码打开 EBR（T3/T4/T5）则全部守恒。

## 运行命令

```bash
node tests.js   # 正确性: 单线程/对拍/SPSC/MPMC/版本回绕/不安全对照
node bench.js   # 吞吐与内存: 100 万 push + 100 万 pop, 1P1C/2P2C/4P4C/8P8C
```

环境：Node v18.19.1，16 核。

## 测试数据（实测输出）

### 正确性与守恒（node tests.js）

```
T1 单线程: 200 万次混合 push/pop 与数组模型逐步对拍
  pop 比对 999825 次, 栈底排空 350 个, 顺序一致
T2 对拍: 与加锁栈跑同一随机操作序列 (seed=20261005, 100 万操作)
  push=500151 pop=499849 逐值比对全部一致; 最终栈内容+顺序一致 (302 项) -> OK
[T3 SPSC 1P1C x 50万]  pushed=500000  popped=500000  remaining=0 dup=0 lost=0 -> OK
[T4 MPMC 4P4C x 100万] pushed=1000000 popped=1000000 remaining=0 dup=0 lost=0 -> OK
[T5 版本回绕 tagBits=2, 4P4C x 100万, EBR 开] pushed=1000000 popped=1000000
   remaining=0 dup=0 lost=0 -> OK
[T6 无EBR tagBits=2, 4P4C] pushed=400000 popped=400003 remaining=401024
   dup=687943 lost=286916 -> CONSERVATION VIOLATED (预期复现, 证明回收时机必要性)
```

守恒校验方式：每个元素 id 唯一（生产者号 × 块大小 + 序号），消费者把弹出的 id
写入共享位图（`Atomics.exchange` 测重），主线程最后排空栈中剩余元素，校验
`popped ∪ remaining == pushed` 且无重复、无越界。

### 对拍（T2 细节）

加锁实现为 `LockedStack`（`Atomics` 自旋锁 + 数组）。同一种子 PRNG 生成
100 万次随机 push/pop，两边逐值比对 pop 结果，最后逐元素排空比对内容与顺序：
0 处不一致。

### 吞吐与内存峰值（node bench.js，100 万 push + 100 万 pop/轮）

| 配置 | 耗时 | 吞吐 | RSS 峰值 | 节点槽+位图 | 守恒 |
|---|---|---|---|---|---|
| 1P+1C  | 580.2 ms | 344.71 万 ops/s |  85.4 MB | 8.68 MB | OK |
| 2P+2C  | 549.5 ms | 363.97 万 ops/s | 119.0 MB | 8.68 MB | OK |
| 4P+4C  | 590.1 ms | 338.95 万 ops/s | 178.4 MB | 8.68 MB | OK |
| 8P+8C  | 766.9 ms | 260.78 万 ops/s | 291.2 MB | 8.68 MB | OK |

- 节点槽为定长预分配（约 100 万节点 × 8 B），运行期**零堆分配**；RSS 峰值随
  worker 数增长主要是每线程的 V8 堆（约 30 MB/线程），与栈本身无关。
- 每轮结束均通过守恒校验：unique=1000000, remaining=0, dup=0。

## 文件

- `stack.js`  — 库：标签指针 Treiber 栈 + 3-epoch EBR + 标签空闲链表分配器 + `LockedStack`
- `worker.js` — 压测/测试共用的生产者、消费者角色
- `tests.js`  — T1–T6 正确性套件
- `bench.js`  — 吞吐与内存峰值基准

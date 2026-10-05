# 并发限制器：令牌管理实现与自测报告

语言：Python 3（仅标准库：`threading` / `contextlib` / `time` / `itertools` / `collections`）
环境：Python 3.12.3，Linux，共享主机（测试时宿主机负载 4–6，耗时数据含噪声，见文末说明）

## 运行命令

```bash
python3 concurrency_limiter/selftest.py    # 退出码 0 = 全部通过
```

## 设计（如何保证"不忘还、不还两次"）

- **令牌即不可变元组** `(limiter_id, serial)`：`serial` 单调递增、永不复用；
  旧引用不可能误命中新租约（ABA 安全）。不可变对象不存在"归还后被改写状态"。
- **单锁台账**：`available / issued / returned / 持有集合` 全部在同一把锁内更新，
  守恒恒等式 `issued == returned + held`、`held == capacity - available` 恒成立，
  `audit()` 每次调用就地断言。
- **唯一归还点**：`lease()` 上下文管理器在 `finally` 中归还，正常/异常/取消路径共用，
  结构上不可能漏还或还两次；`release()` 幂等且校验令牌身份。
- **重复/伪造归还识别**：非元组、`limiter_id` 不符（跨实例）、`serial <= last_serial`
  但不在持有集合（重复归还）、`serial > last_serial`（从未发放）分别拒绝并计入
  `duplicate_returns`，拒绝路径不扰动任何计数。
- **等待可中断**：`acquire(timeout=..., cancel_event=...)`；超时/取消路径不发令牌，
  因此无需归还；等待登记在放弃时撤销，被弹出则说明令牌已留在账上，不丢。
- **等待队列手写**（per-waiter 锁），不用 `threading.Condition`：实测 CPython 3.12
  多线程竞争下 `Condition` 的 `_is_owned()`（对已持有锁做 `acquire(False)`）会使
  吞吐从 ~10M 跌到 ~4 万 ops/s；同理热路径不分配 GC 追踪的类实例（~8.7M -> ~5 万
  ops/s），故令牌选用走 freelist 的普通元组。

## 1. 各路径令牌数（capacity=2，实测）

| 路径 | 时点 | available | held | issued | returned |
|---|---|---|---|---|---|
| 初始 | — | 2 | 0 | 0 | 0 |
| 正常 | 持牌中 / 归还后 | 1 / **2** | 1 / 0 | 1 | 0 / 1 |
| 异常（业务抛错） | 持牌中 / 归还后 | 1 / **2** | 1 / 0 | 2 | 1 / 2 |
| 取消 A（持牌时被 KeyboardInterrupt） | 持牌中 / 归还后 | 1 / **2** | 1 / 0 | 3 | 2 / 3 |
| 取消 B（等待时 cancel_event） | 占满 / 取消后 / 全还 | 0 / **0** / 2 | 2 / 2 / 0 | 5 | 3 / 3 / 5 |
| 超时（acquire timeout=0.05） | 占满 / 超时后 | 0 / **0** | 2 / 2 | 7 | 5 |
| 超时（lease timeout=0.02） | 超时后 | **0** | 2 | 9 | 7 |
| 全部路径结束 | — | **2** | 0 | 9 | 9 |

取消/超时路径不发令牌（available 不变、不计 issued），持牌取消路径由 `finally` 归还。

## 2. 重复归还检出（capacity=1，实测）

| 操作 | 结果 | duplicate_returns |
|---|---|---|
| 正常归还 | 接受 | 0 |
| 同一令牌第 2 次归还 | 拒绝：`token #1 already released (double return)` | 1 |
| 同一令牌第 3 次归还 | 拒绝：同上 | 2 |
| 归还 `None` | 拒绝：`expected a Token (limiter_id, serial)` | 3 |
| 归还非 Token 对象 | 拒绝：同上 | 4 |
| 跨实例归还 | 拒绝：`token #1 belongs to another limiter` | 5 |

检出后账目不变：`available=1, issued=1, returned=1`。

## 3. 守恒核算（capacity=3，1000 次混合正常/异常路径，实测）

```
issued(1000) == returned(1000) + held(0)   -> True
held(0)      == capacity(3) - available(3) -> True
issued == 正常(667) + 异常(333) = 1000     -> True
```

## 4. 并发压测守恒校验（32 线程 × 5000 次，capacity=16，实测）

- 总操作 160,000 次，每次归还后**故意再归还一次**（必须全部被拒）。
- 结果：`issued=160000, returned=160000, held=0, available=16,
  duplicate_returns=160000`（重复归还 100% 检出）。
- 峰值并发持有 = 16 = capacity，**从未越限**；32 个线程全部正常结束。
- 两轮耗时：17.1s / 15.1s（含宿主机噪声）。

## 5. 100 万次获取归还：耗时与内存（实测两轮）

| 场景 | 第 1 轮 | 第 2 轮 | 上界（断言） |
|---|---|---|---|
| 单线程 1,000,000 次 | 2.366s（42.3 万/s，2.37µs/次） | 2.137s（46.8 万/s，2.14µs/次） | < 30s |
| 64 线程 1,000,000 次 | 71.1s | 75.3s | < 300s |
| 内存（tracemalloc 峰值） | 196.9 KiB | 196.7 KiB | < 5 MiB |
| 进程峰值 RSS | 13.8 MiB | 13.7 MiB | — |

- 两轮结束台账均为 `issued=returned=1,000,000, held=0, available=256`，守恒成立。
- 内存上界分析：限制器常驻状态为 O(capacity)（持有集合 + 等待队列），
  与操作次数无关；1M 次操作的令牌为元组，即生即灭，实测峰值 ~0.2 MiB ≪ 5 MiB。
- 宿主机噪声说明：空载时单线程实测 0.08s/20 万次（≈250 万 ops/s）、
  64 线程 0.09s/20 万次（≈220 万 ops/s）；上表为负载 4–6 下的实测，
  故多线程耗时上界取宽松值 300s，单线程上界 30s 对噪声不敏感。

## 覆盖矩阵

| 情形 | 场景 |
|---|---|
| 正常路径 | §1 正常行、§3、§4、§5 |
| 异常路径（业务抛错） | §1 异常行、§3（333 次） |
| 取消路径 | §1 取消 A（持牌取消）/ 取消 B（等待取消） |
| 超时路径 | §1 acquire 超时 / lease 超时 |
| 重复归还 | §2（5 类检出）、§4（16 万次全检出） |
| 高并发守恒 | §4（capacity=16 < 32 线程，真实等待竞争） |
| 百万次耗时/内存 | §5 |

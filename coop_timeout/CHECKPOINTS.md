# 任务超时的协作式中断 —— 检查点、清理与边界用例

纯 Python 3 标准库实现；时间完全由调用方注入（`VirtualClock` / `SystemClock`），
因此所有正确性结论都是确定性的，性能数据用墙钟另行测量。

源码：
- `coop_timeout/coop.py` —— 库：时钟、取消令牌、任务上下文、检查点、runner
- `coop_timeout/demo.py` —— 标准任务 + 17 个中断点场景矩阵 + 对拍
- `coop_timeout/tests.py` —— 21 个 unittest 自测
- `coop_timeout/bench.py` —— 10 万次超时场景分布与调度耗时

## 1. 核心模型

任务被写成一串**原子步骤**（`acquire` / `step(work)` / `release` / `commit`）。
超时与取消只做两件事：把信号**闩锁**（latch）住；信号只在**安全检查点**被
采样和响应。绝不在步骤执行到一半时强制打断。

状态机：

```
RUNNING  --(检查点采样到信号，抛内部中断)-->  CLEANING  --(全部释放完)-->  SEALED
   |                                            |
   +--------------(任务返回 / 失败)------------>+
SEALED 之后任何信号无效，已封存结果永不改变
```

信号有两种，检查点同点同时出现时按固定优先级响应：**cancel 优先于 timeout**
（两者都会记录在 `result.signals`，可区分）。超时判定为 `now >= deadline`，
即**到达边界值就算超时**。

## 2. 安全检查点的位置与响应规则

| 检查点 | 位置 | RUNNING 下的响应规则 |
|---|---|---|
| 显式检查点 | `ctx.checkpoint("pre-commit")` | 采样；有信号则转 CLEANING 并抛内部中断 |
| 步骤检查点 | `ctx.step()` 自动在步骤前后各放一个 | 同上；步骤体本身不是检查点 |
| 获取检查点 | `ctx.acquire()` 前后各一个 | 同上；获取体本身不是检查点 |
| 清理检查点 | 每次 `release` 完成之后 | **只观察、永不抛出、永不中止清理**；新信号记入 `cleanup.observations` |

响应规则细则：

1. 信号在非检查点到达只被闩锁：当前步骤（含 `acquire`、每一次 `release`）
   一定完整执行，副作用全部落地，到下一个检查点才响应。
2. 中断通过内部异常 `_Interrupt`（继承 `Exception`，不是裸 `BaseException`
   的特殊机制）在检查点处抛出；任务体的普通 `except Exception` 可以选择不捕获
   ——如果误捕获，runner 不再重试（由编写者保证检查点契约）。
3. 响应点被记录为 `result.stage`（在哪个检查点决定了结果）；清理阶段的检查点
   不会改写它。
4. 清理检查点对**新出现**的信号类型做去重观察：清理中第二个信号可被观测，
   重复的同类型信号（令牌是幂等闩锁）不产生观察记录。
5. `commit`（任务体返回）之后的收尾释放不可中断：deadline 只约束任务体；
   一旦返回 `ok`，即使收尾释放的虚拟时间越过 deadline，结果仍是 `ok`。
6. 任务体的**第一个检查点之前**是一个无检查点区间——必须由编写者放置首个
   检查点（通常是 `pre-acquire-*` 或 `start`），库不替任务决定“哪里安全”。

禁止放检查点/不可能被响应的位置：步骤体中间、资源获取中间、**单次 release
中间**、commit 之后。

## 3. 清理语义

- 无论结果是 ok / timeout / cancelled / failed，runner 都在封存结果前排空
  资源栈。
- 释放顺序 **LIFO**；每个资源 **exactly-once**；单次释放是原子的、不会被任何
  信号劈开。
- 某个 `release` 抛异常：异常被捕获为该资源的 `release_error`，**不影响**
  其余资源继续释放（场景 N）。
- 清理中再次中断：释放照样全部完成，新信号只在清理报告中留下观察记录
  （场景 J / M；重复信号幂等见自测 `test_repeated_same_signal_...`）。

## 4. 中断点落在各阶段时的清理结果

标准任务时间线（虚拟 ns，acquire 各 2，w1/w2/w3 = 6/10/10，t=30 commit）：

```
pre-acq-R1@0  acq R1 0..2  pre-acq-R2@2  acq R2 2..4
pre-w1@4  w1 4..10  pre-w2@10  w2 10..20  pre-w3@20  w3 20..30
pre-commit@30  commit@30  release R2  release R1
```

下表为 `python3 -m coop_timeout.demo` 实际输出（快照见
`docs/demo_output.txt`）。`ovsh` = finished − deadline（响应延迟，虚拟 ns）；
cleanup-observations = 清理阶段新观察到的信号。

| 场景（中断点 / 触发） | outcome | 响应检查点 | 已释放资源 | signals | ovsh | 清理中观察 |
|---|---|---|---|---|---|---|
| A1 t=0 刚好超时 | timeout | pre-acquire-R1 | — | timeout | 0 | — |
| A2 deadline=2 | timeout | post-acquire-R1 | R1 | timeout | 0 | — |
| B 获取 R2 中途超时 (d=3) | timeout | post-acquire-R2 | R1,R2 | timeout | 1 | — |
| C 边界 t==deadline (d=4) | timeout | post-acquire-R2 | R1,R2 | timeout | 0 | — |
| D w1 中途超时 (d=7) | timeout | post-w1 | R1,R2 | timeout | 3 | — |
| E w2 中途超时 (d=15) | timeout | post-w2 | R1,R2 | timeout | 5 | — |
| F 末步结束刚好超时 (d=30) | timeout | post-w3 | R1,R2 | timeout | 0 | — |
| G0 commit 前取消 | cancelled | pre-commit | R1,R2 | cancel | — | — |
| G w2 前取消 | cancelled | pre-w2 | R1,R2 | cancel | — | — |
| H w1 中途收到取消 | cancelled | post-w1 | R1,R2 | cancel | — | — |
| I 任务自身失败 (w2) | failed | w2 | R1,R2 | — | — | — |
| J 清理中再次中断（新信号） | timeout | post-w2 | R1,R2 | timeout,cancel | 5 | cancel@post-release-R2 |
| K 正常完成 | ok | commit | R1,R2 | — | — | — |
| K0 差一点未超时 (d=31) | ok | commit | R1,R2 | — | — | — |
| L 运行前已取消 | cancelled | pre-acquire-R1 | — | cancel | — | — |
| M 清理中越过 deadline | cancelled | pre-w2 | R1,R2 | cancel,timeout | 10 | timeout@post-release-R2 |
| N 释放函数自身抛错 | ok | commit | R1,R2 | — | — | —（R2 记录 release_error） |

关键事实：B 证明“获取中途到信号”也会先把资源注册好再响应，因此 R2 一样被
清理；F/G0 证明响应点在 commit 前时 `commit` 事件绝不发生；所有行的资源
最终都是 all_released（“已释放”= 已执行并记账 exactly-once；N 的 R2 带错）。

## 5. 中断可观测性：调用方如何区分三种中断/失败

`run(...)` 返回封存的 `TaskResult`：

| 字段 | 含义 |
|---|---|
| `outcome` | `ok` / `timeout` / `cancelled` / `failed` 四值互斥 |
| `stage` | 决定结果的检查点（中断响应点 / 失败步骤 / commit） |
| `value` / `error` | ok 的返回值；failed 的异常类型与消息 |
| `signals` | 已响应/观察到的信号序列，如 `(timeout, cancel)` |
| `overshoot` | timeout 时 finished − deadline（协作式响应延迟） |
| `deadline`, `started_at`, `finished_at` | 时间事实（注入时钟） |
| `cleanup.records` | 每个资源的获取/释放时刻、释放错误 |
| `cleanup.observations` | 清理阶段新观察到的（信号, 时刻, 检查点） |
| `cleanup.all_released` / `.release_errors` | 清理完整性断言入口 |

区分要点：超时看 `outcome=="timeout"`（且 signals 含 timeout）；主动取消看
`cancelled`；任务自身失败看 `failed` + `error`，且失败同样触发完整清理。

## 6. 已完成任务再被中断：对拍数据

`demo.main()` 用相同输入各跑一遍：基线 K（正常完成）与 K'（封存后再
`token.cancel()`）。对拍（实际输出）：

```
baseline        : ('ok', 'RESULT', None, 'commit', 0, 30, None, (), None)
after-late-cancel: ('ok', 'RESULT', None, 'commit', 0, 30, None, (), None) (token.cancelled=True)
result identical : True | cleanup identical: True | event log identical: True
```

九元组是 `TaskResult.key()`（outcome/value/error/stage/起止时刻/deadline/
signals/overshoot）。取消令牌虽然已置位，但任务已 SEALED，结果、清理记录、
事件日志三者逐字一致。失败任务（I）封存后取消的对拍在自测
`test_cancel_after_failure_does_not_change_result` 中同样校验。

## 7. 10 万次超时场景：响应延迟分布与调度耗时

`python3 -m coop_timeout.bench`（每次 10 万任务、两个资源、6 个 8–16ns 的
原子步骤，deadline 随机落在任务体内部；机器实际输出快照见
`docs/bench_output.txt`）。

两种“延迟”分开报告：

1. **协作式响应延迟（逻辑口径，注入时间）**：overshoot = 清理完成时刻 −
   deadline，即“把当前原子步骤跑完”带来的确定性延迟，与机器速度无关。
2. **调度耗时（墙钟口径）**：runner/检查点机制本身的 CPU 成本。

本机一次运行结果（3 次重复取中位数）：

```
10 万任务墙钟：  best 678 ms / median 688 ms / worst 698 ms
调度成本：       6.88 us/任务（约 14.5 万超时任务/秒）
每任务检查点：   13.22；每任务释放：2.00

overshoot(虚拟ns)：min 0  mean 5.83  pstdev 3.88
  p50=6  p90=11  p95=13  p99=14  p99.9=15  max=15
直方图(bucket=4ns)：
  [0,4)   32620  ########################################
  [4,8)   33706  ########################################
  [8,12)  24375  #############################
  [12,16)  9299  ###########
响应检查点分布：post-w3 46935 / post-w2 42851 / post-w4 7912 / post-w1 2217 / post-w5 85

空闲检查点成本：115.2 ns/checkpoint（约 868 万检查点/秒）
```

分布有界：overshoot 不可能超过一个原子步骤的最大时长（16ns），这正是协作式
语义的延迟上界——**延迟来自“跑完当前步骤”，不是抢占抖动**。

## 8. 边界用例覆盖

| 边界情形 | 位置 |
|---|---|
| 未超时（deadline 远大于结束时刻） | `tests.py` BoundaryTests.test_not_timed_out |
| 刚好超时（now == deadline 判定超时；d=30 超时 / d=31 未超时对） | K0、F、test_exactly_at_deadline_is_timeout |
| 清理中再次中断——新信号 | 场景 J、M；ReInterruptDuringCleanupTests |
| 清理中再次中断——重复信号幂等 | test_repeated_same_signal_during_cleanup_is_idempotent_no_observation |
| 已完成再中断（ok 与 failed 各一） | CompletedThenInterruptedTests + demo A/B |
| 运行前已取消 | 场景 L、test_cancel_before_start |
| 同检查点 cancel+timeout 优先级 | test_cancel_and_timeout_at_same_checkpoint |
| 步骤中途到信号：步骤完整执行 | test_signal_during_step_is_latched_step_completes |
| 获取中途到信号：资源注册并清理 | test_signal_during_acquire_latched_resource_registered_and_cleaned |
| release 中途到信号：释放完整执行、exactly-once、LIFO | AtomicityTests 三个用例 |
| release 自身抛错不阻塞其余释放 | 场景 N、test_release_failure_does_not_block_remaining_releases |
| timeout/cancelled/failed 可区分 | DistinguishabilityTests |

## 9. 运行命令

```bash
# 自测（21 个用例）
python3 -m unittest coop_timeout.tests -v

# 中断点 × 清理结果矩阵 + 已完成再中断对拍
python3 -m coop_timeout.demo

# 10 万次超时：延迟分布 + 调度耗时
python3 -m coop_timeout.bench
```

无需安装任何第三方依赖，Python 3.8+ 即可（dataclasses / contextlib）。

# 预算消耗序列（retry budget-consumption sequences）

时间单位：tick（仓库统一取 1 tick = 1 ms）。start/end 为自请求进入起的绝对 tick 偏移；duration 为该项消耗。所有序列由真实库执行生成。

覆盖首段超时、首段重试后成功、末段超时、排队耗尽、全部成功，并附一个“各段各自兜底”的错误做法对照。

## S1 首段持续超时（3 段等权，预算 100，无 backoff，最多 12 次）

每次重试只从剩余预算里按权重再切一份，序列单调收缩：33→22→15→10→6→4→3→2→1→1→1。

| # | 项 | start | duration | end(cum) | 说明 |
|---|---|---:|---:|---:|---|
| 1 | A-try1 | 0 | 33 | 33 | timeout raised by handler: scripted timeout |
| 2 | A-try2 | 33 | 22 | 55 | timeout raised by handler: scripted timeout |
| 3 | A-try3 | 55 | 15 | 70 | timeout raised by handler: scripted timeout |
| 4 | A-try4 | 70 | 10 | 80 | timeout raised by handler: scripted timeout |
| 5 | A-try5 | 80 | 6 | 86 | timeout raised by handler: scripted timeout |
| 6 | A-try6 | 86 | 4 | 90 | timeout raised by handler: scripted timeout |
| 7 | A-try7 | 90 | 3 | 93 | timeout raised by handler: scripted timeout |
| 8 | A-try8 | 93 | 2 | 95 | timeout raised by handler: scripted timeout |
| 9 | A-try9 | 95 | 1 | 96 | timeout raised by handler: scripted timeout |
| 10 | A-try10 | 96 | 1 | 97 | timeout raised by handler: scripted timeout |
| 11 | A-try11 | 97 | 1 | 98 | timeout raised by handler: scripted timeout |

观察到的实际耗时合计：**98**；计入预算口径（封顶 100）：**used=98**。

- 结果：`budget_exhausted`，共派发 11 次尝试，全部集中在首段，B/C 从未被派发。
- 累计消耗 98，未超过总预算，更没有翻倍（重试次数上限 12 不等于 12×33）。
- 第 12 次需要的切片 floor(2/3)=0 < min_slice=1，派发前即判耗尽。

## S2 首段持续超时 + 每次重试 backoff 2 tick

backoff 与执行尝试共用同一份预算，先扣 backoff，再从剩余里切下一片。

| # | 项 | start | duration | end(cum) | 说明 |
|---|---|---:|---:|---:|---|
| 1 | A-try1 | 0 | 33 | 33 | timeout raised by handler: scripted timeout |
| 2 | backoff | 33 | 2 | 35 | retry backoff 1/11 |
| 3 | A-try2 | 35 | 21 | 56 | timeout raised by handler: scripted timeout |
| 4 | backoff | 56 | 2 | 58 | retry backoff 2/11 |
| 5 | A-try3 | 58 | 14 | 72 | timeout raised by handler: scripted timeout |
| 6 | backoff | 72 | 2 | 74 | retry backoff 3/11 |
| 7 | A-try4 | 74 | 8 | 82 | timeout raised by handler: scripted timeout |
| 8 | backoff | 82 | 2 | 84 | retry backoff 4/11 |
| 9 | A-try5 | 84 | 5 | 89 | timeout raised by handler: scripted timeout |
| 10 | backoff | 89 | 2 | 91 | retry backoff 5/11 |
| 11 | A-try6 | 91 | 3 | 94 | timeout raised by handler: scripted timeout |
| 12 | backoff | 94 | 2 | 96 | retry backoff 6/11 |
| 13 | A-try7 | 96 | 1 | 97 | timeout raised by handler: scripted timeout |
| 14 | backoff | 97 | 2 | 99 | retry backoff 7/11 |

观察到的实际耗时合计：**99**；计入预算口径（封顶 100）：**used=99**。

- 结果：`budget_exhausted`，7 次执行尝试 + 7 次 backoff，消耗 99 / 100。
- 下一次 backoff(2) 需要剩余 0 tick 支撑，派发前判耗尽。

## S3 错误做法对照：每次重试都拿一份新的本地超时 40 tick（backoff 5）

这是“指望各段各自超时兜底”的典型写法：每段调用方按总预算的 40% 配置下游超时，且重试不感知全局预算。序列如下（纯算术模拟，非本库执行）。

| # | 项 | start | duration | end(cum) | 说明 |
|---|---|---:|---:|---:|---|
| 1 | try1 (local 40) | 0 | 40 | 40 | fresh local timeout, global budget ignored |
| 2 | backoff | 40 | 5 | 45 | retry backoff |
| 3 | try2 (local 40) | 45 | 40 | 85 | fresh local timeout, global budget ignored |
| 4 | backoff | 85 | 5 | 90 | retry backoff |
| 5 | try3 (local 40) | 90 | 40 | 130 | fresh local timeout, global budget ignored |

累计消耗：**130** / 100

> 没有全局闸门时链实际跑到 **130 tick**，超出全局预算 **30 tick**（30.0%）。

- 本库对照：全局闸门在 end=100 处强制判 `budget_exhausted`，第 3 次尝试最多只拿到 10 tick 而不是 40。
- 这就是“重试次数让总时长翻倍”的来源：3 次尝试 × 40 + 2 × 5 = 130。

## S4 首段第一次超时、第二次成功（重试消耗同一份预算）

首次尝试超时 25 tick + backoff 2 + 重试成功 10 + B 10 + C 15。

| # | 项 | start | duration | end(cum) | 说明 |
|---|---|---:|---:|---:|---|
| 1 | A-try1 | 0 | 25 | 25 | timeout raised by handler: scripted timeout |
| 2 | backoff | 25 | 2 | 27 | retry backoff 1/2 |
| 3 | A-try2 | 27 | 10 | 37 | ok |
| 4 | B-try1 | 37 | 10 | 47 | ok |
| 5 | C-try1 | 47 | 15 | 62 | ok |

观察到的实际耗时合计：**62**；计入预算口径（封顶 100）：**used=62**。

- 结果：`success`，消耗 62，剩余 38 原样保留，不会因为重试过就吃掉后续段的额度。

## S5 末段超时（前两段各花 10，末段独占剩余权重）

末段剩余权重占 100%，第一次尝试拿到全部 80 tick；用尽后第二次切片为 0，提前停止。

| # | 项 | start | duration | end(cum) | 说明 |
|---|---|---:|---:|---:|---|
| 1 | A-try1 | 0 | 10 | 10 | ok |
| 2 | B-try1 | 10 | 10 | 20 | ok |
| 3 | C-try1 | 20 | 80 | 100 | timeout raised by handler: scripted timeout |

观察到的实际耗时合计：**100**；计入预算口径（封顶 100）：**used=100**。

- 结果：`budget_exhausted`（发生在第 2 次派发前），共 3 次尝试，消耗 100 / 100。

## S6 排队先吃掉 95 tick（排队计入同一口径）

Budget 在入队前创建；派发前采样，剩余 5 tick 时 floor(5/2)=2 的首段切片虽然够，该段 10 tick 后返回（越过 2 tick 切片），运行时在返回点强制判耗尽，后续段无预算。

| # | 项 | start | duration | end(cum) | 说明 |
|---|---|---:|---:|---:|---|
| 1 | queue | 0 | 95 | 95 | pre-chain queueing, charged to the same budget |
| 2 | A-try1 | 95 | 10 | 105 | overran slice |

观察到的实际耗时合计：**105**；计入预算口径（封顶 100）：**used=100**。

> 观察耗时超过预算 5 tick：原因是 handler 无视其切片；运行时在返回点立即判定 `budget_exhausted`（`enforce_slice`），后续段未派发，预算口径 used 封顶为 100。

- 结果：`budget_exhausted`，stage 'A' ignored its 2-tick slice and used 10 tick(s)，共 1 次尝试。

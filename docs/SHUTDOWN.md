# 优雅停机设计说明

库：`graceful_shutdown/`（纯标准库，Python 3.10+）。时钟由测试注入：
`SimEngine` 用虚拟时钟（确定性、可跑 10 万任务），`ThreadEngine` 用
`time.monotonic()`（真实墙钟）。两个引擎共享同一套停机编排
（`shutdown.run_shutdown`）、同一套处置落库逻辑（`model.apply_disposition`），
语义完全一致。

## 停机流程（四阶段，每步时限明确）

| 阶段 | 名称 | 动作 | 时限（配置项） |
|---|---|---|---|
| P1 | stop-accepting | 关闭入口；之后提交的任务记为 `REJECTED`，永不执行 | 瞬时（预算 0） |
| P2 | drain-inflight | 发出取消信号；运行中任务在**当前步骤结束的安全点**进入处置；排队任务立即以 0 步进入处置 | `drain_timeout` |
| P3 | cleanup | 等待处置（写检查点 / 回滚 / 标记失败）完成 | `cleanup_timeout` |
| P4 | force-exit | 最后宽限；到期仍未完成的任务一律记为 `ABANDONED` 并返回 | `force_wait` |

硬截止 = `drain_timeout + cleanup_timeout + force_wait`。守时是结构性的：
每个阶段最多等自己的预算，P4 以"放弃剩余任务"收尾，因此调用必然在
硬截止前返回（真实时钟下另有 `punctuality_slack` 调度抖动余量）。
任何阶段若提前无事可等（无在途、无待处置）会提前返回，不会干等。

## 未完成任务的处置（确定性）

| 策略 | 动作 | 结果（可核对） |
|---|---|---|
| `CHECKPOINT` | 持久化已完成步数 k | `store.checkpoints[tid]=k`，状态 `CHECKPOINTED(k)`，可从断点恢复 |
| `ROLLBACK` | 按逆序补偿已应用的 k 步 | `store.compensations` 恰好 k 条逆序记录，状态 `ROLLED_BACK(k)`，状态回到任务前 |
| `FAIL` | 标记失败，保留已应用效果 | `store.failures[tid]=k`，状态 `FAILED(k)`，留待人工/自动 triage |
| （兜底） | P4 到期仍未完成处置（含卡死任务） | 状态 `ABANDONED(k)`，**无任何持久化足迹**，需运维介入 |
| （入口关闭后） | 停机信号后到达的任务 | 状态 `REJECTED(0)`，零副作用 |

所有处置先写预写日志（`Ledger`）再落持久状态再发布结果；
`ResultTable` 首写生效，一个任务有且仅有一个终态。

## 守时证据（计时数据）

### 真实进程硬退出（`demo_real_exit.py`，墙钟）

预算 1.6s（drain 0.6 + cleanup 0.6 + force 0.4），含 2 个卡死任务：

- 实际总耗时 1.6005s，超调 0.51ms（在 slack 0.2s 内），`punctual=True`
- 各相位：drain 0.6001s / cleanup 0.6001s / force 0.4002s
- 结果：checkpointed 4、rolled_back 1、abandoned 2（即两个卡死任务）
- 进程以 `os._exit(42)` 硬退出，退出码 42（卡死线程无法拖住进程）

### 10 万在途任务（`bench.py`，虚拟时钟，workers=64）

停机信号发出时在途 ≈ 100,000。三个预算变体、共 50 次试验，**全部守时**：

| 变体 | 预算 (drain/cleanup/force) | 停机耗时分布 min/p50/p95/max | 放弃任务数 | 内存峰值 (tracemalloc) |
|---|---|---|---|---|
| A 宽松 | 0.1 / 2.0 / 0.5 s | 0.9368 / 0.9386 / 0.9392 / 0.9397 s | 0 | 90.9 MB |
| B 紧张 | 0.05 / 0.3 / 0.1 s | 恰为预算 0.4500 s（每次） | 51,952–52,214 | 87.6 MB |
| C 紧张+50 卡死 | 0.05 / 0.3 / 0.1 s | 恰为预算 0.4500 s（每次） | 51,964–52,214（含 50 个卡死） | 87.6 MB |

- A 变体相位均值：drain 0.1000s（打满）、cleanup 0.8383s（处置完即提前结束）、force 0s（无事可等）
- 仿真自身墙钟开销：p50 ≈ 0.26–0.53s/次（10 万任务），最大 1.05s
- 进程 RSS 峰值 665.9 MB（整个基准进程，含 10 万任务基线全量对拍）
- 原始数据：`results/bench_100k.json`

## 停机结果 vs 全量跑完（对拍）

`reconcile.reconcile(full, shutdown, store=..., resumed=...)` 逐任务解释差异，
并校验不变量。差异类别（每一类都有确定性解释）：

| 类别 | 含义 |
|---|---|
| `match` | 截止前已完成，与基线一致 |
| `checkpointed` | 断点 k 已持久化，可恢复 |
| `resumed-match` | 断点任务恢复后跑完，合并结果 == 基线 |
| `rolled-back` | k 步已逆序补偿，状态回到任务前 |
| `failed` | 标记失败，k 步效果保留待 triage |
| `abandoned` | 强退，无持久化结果 |
| `abandoned-stuck` | 卡死任务基线也不会完成，强退是唯一结局 |
| `rejected` | 入口关闭后到达，未启动 |

不变量：检查点集合 == CHECKPOINTED 任务集合；每任务补偿条数 == 已应用步数
且逆序；失败集合一致；ABANDONED 任务零持久化足迹；缺终态/无法解释的差异
即违例。10 万任务 A 变体 trial 0 对拍结果：`ok=True`
（checkpointed 60,011 / rolled-back 29,921 / failed 10,068，零违例）。

## 边界用例（`selftest.py`，15 个测试）

- 无在途任务：停机瞬时完成（耗时 0），守时
- 少量在途：三种策略各 2 个任务，验证处置结果、持久存储、逆序补偿、预写日志
- 大量在途：5,000 任务全部处置完成、零放弃、守时，并与基线对拍
- 任务卡死：3 个卡死任务在硬截止被 ABANDONED，总耗时恰好等于预算
- 处置超时：cleanup 预算不足时 10 个任务全部 ABANDONED，耗时恰为预算
- 入口关闭：迟到任务记 REJECTED
- 排队任务：0 步任务的处置（0 条补偿、0 步检查点）
- 对拍与恢复：断点任务恢复后合并结果 == 基线；篡改存储会被对拍抓出
- 真实硬退出：子进程退出码 42，耗时 ≤ 预算 + 0.5s

## 运行命令

```bash
python3 -m unittest selftest -v   # 自测（15 个用例）
python3 bench.py                  # 10 万在途基准，写 results/bench_100k.json
python3 demo_real_exit.py         # 真实硬退出演示，退出码 42
```

# 线程池拒绝策略与通知 — 交付报告

语言：Python 3（仅标准库：`threading` / `collections` / `enum` / `dataclasses`）

## 文件

| 文件 | 说明 |
|---|---|
| `src/threadpool.py` | 有界线程池库：4 种拒绝策略、显式拒绝通知、关闭状态机、计数守恒台账 |
| `tests/test_threadpool.py` | 12 个自测（unittest），覆盖队列未满 / 已满 / 关闭中 / 关闭后 / 守恒 |
| `benchmark.py` | 10 万次提交、队列容量 100 的耗时与拒绝率分布基准 |
| `benchmark_output.txt` / `tests_output.txt` | 实测输出存档 |

## 运行命令

```bash
python3 -m unittest discover -s tests -v   # 自测（12 个用例）
python3 benchmark.py                       # 10 万提交基准（约 10s）
```

## 拒绝策略说明

队列满（`len(queue) >= capacity`）且池处于 RUNNING 时按策略处理：

| 策略 | 行为 | 调用方拿到的通知 |
|---|---|---|
| `ABORT` | 拒绝新任务 | **抛 `RejectedExecutionError`**，异常携带 `task_id` 与 `reason=QUEUE_FULL` |
| `DISCARD` | 丢弃新任务 | 返回 `SubmitResult(accepted=False, reason=QUEUE_FULL)`，不抛异常 |
| `CALLER_RUNS` | 由提交线程当场执行 | 返回 `SubmitResult(accepted=True, executed_inline=True)`，计入 completed |
| `DISCARD_OLDEST` | 驱逐队首最旧任务、接纳新任务 | 新任务 `accepted=True`；被驱逐任务计入 rejected（`DISCARDED_OLDEST`），写入 `pool.rejection_log` 并触发 `on_rejected` 回调 |

**任何拒绝都不会静默发生**，三条显式通知通道：

1. 返回值 / 异常：每次 `submit()` 要么返回带 `reason` 的 `SubmitResult`，要么（ABORT）抛带 `reason` 的异常；
2. `pool.rejection_log`：每条拒绝记录 `(task_id, reason, detail)`；
3. `on_rejected` 回调：构造时注入，每次拒绝（含驱逐、shutdown_now 取消）实时触发。

## 关闭各阶段的提交结果

状态机：`RUNNING → SHUTDOWN →（可选 STOP）→ TERMINATED`

| 阶段 | 提交结果（ABORT 抛异常；其余策略返回 `accepted=False`） | 已在池中的任务 |
|---|---|---|
| RUNNING | 正常受理；队列满按策略处理 | 正常执行 |
| SHUTDOWN（`shutdown()` 优雅关闭） | 拒绝，`reason=SHUTTING_DOWN` | 队列排空、全部执行完后才 TERMINATED |
| STOP（`shutdown_now()` 立即关闭） | 拒绝，`reason=STOPPING` | 队列中任务被取消，计入 rejected（`SHUTDOWN_DRAINED`），运行中任务执行完 |
| TERMINATED | 拒绝，`reason=TERMINATED` | 无残留 |

对应自测：`test_submit_during_graceful_shutdown_rejected_then_drain`、
`test_shutdown_now_cancels_queued_with_reason`、
`test_discard_policy_returns_result_in_shutdown_phases`。

## 计数守恒核算

不变式（在任意静止点成立，`pool.accounting()` 可快照，`check_conservation()` 校验）：

```
submitted == completed + rejected + queued + running
```

- `rejected` 汇总三类：提交时被拒（队列满 / 关闭中 / 已终止）+ DISCARD_OLDEST 驱逐 + shutdown_now 取消；
- `failed`（任务抛异常）是 `completed` 的子计数，不破坏守恒；
- 终止后 `queued = running = 0`，退化为 `submitted == completed + rejected`。

10 万提交基准的核算（本次实测，每次运行数字略有浮动）：

| 策略 | submitted | completed | rejected | queued+running | 守恒 |
|---|---|---|---|---|---|
| abort | 100000 | 26950 | 73050 | 0 | 26950+73050=100000 ✓ |
| discard | 100000 | 20493 | 79507 | 0 | ✓ |
| caller_runs | 100000 | 100000 | 0 | 0 | ✓ |
| discard_oldest | 100000 | 23356 | 76644 | 0 | ✓ |

自测中的守恒用例：`test_ledger_balanced_with_abort_policy`（8 线程 × 500 提交并发压测，逐次校验）、
`test_ledger_balanced_with_discard_oldest`（每次提交后即时校验）。

## 耗时与拒绝率分布（10 万提交，队列容量 100，workers=4）

任务为小型 CPU 负载（`sum(i*i for i in range(200))`）。实测（CPython 3.12，GIL 下数字有波动）：

| 策略 | 提交耗时 | 吞吐 | completed | rejected | 拒绝率 |
|---|---|---|---|---|---|
| abort | 3.24s | ~31k/s | 26950 | 73050 | 73.05% |
| discard | 2.40s | ~42k/s | 20493 | 79507 | 79.51% |
| caller_runs | 2.18s | ~46k/s | 100000 | 0 | 0.00% |
| discard_oldest | 2.73s | ~37k/s | 23356 | 76644 | 76.64% |

按 10 批 × 1 万提交的拒绝率分布（%，拒绝事件按发生时的提交窗口分桶，含驱逐）：

| 策略 | min | mean | max | stdev |
|---|---|---|---|---|
| abort | 53.85 | 73.05 | 92.31 | 10.45 |
| discard | 67.34 | 79.51 | 88.80 | 6.31 |
| caller_runs | 0.00 | 0.00 | 0.00 | 0.00 |
| discard_oldest | 56.00 | 76.64 | 88.47 | 8.74 |

解读：提交速率远超 4 个 worker 的消费速率，队列容量 100 很快打满，故 ABORT/DISCARD
拒绝率高且随 GIL 调度波动；CALLER_RUNS 用反压把多余任务压回调用线程执行，拒绝率恒为 0，
吞吐反而最高（无入队/拒绝开销）；DISCARD_OLDEST 的"拒绝"全部是被驱逐的旧任务
（`discarded_oldest`），提交方自身始终被受理。

## 自测覆盖矩阵（12 用例全过）

| 情形 | 用例 |
|---|---|
| 队列未满 | `test_all_accepted_none_rejected`、`test_failed_task_still_counts_as_completed` |
| 队列已满 | `test_abort_raises_with_reason`、`test_discard_returns_explicit_result`、`test_caller_runs_executes_on_submitting_thread`、`test_discard_oldest_evicts_head_and_records_it` |
| 关闭中提交 | `test_submit_during_graceful_shutdown_rejected_then_drain`、`test_shutdown_now_cancels_queued_with_reason`、`test_discard_policy_returns_result_in_shutdown_phases` |
| 关闭后提交 | 同上三用例的 TERMINATED 阶段断言 |
| 通知通道 | `test_on_rejected_callback_fires_for_every_rejection` |
| 计数守恒 | `test_ledger_balanced_with_abort_policy`、`test_ledger_balanced_with_discard_oldest`（+ 每个用例内的 `assertConserved`） |

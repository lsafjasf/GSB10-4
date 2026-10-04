# 线程池拒绝策略与通知（Python 3，仅标准库）

## 文件

| 文件 | 说明 |
|---|---|
| `threadpool.py` | 线程池库：`ThreadPool` / `Policy` / `SubmitResult` / `Future` / `PoolStats` |
| `test_threadpool.py` | 12 个自测：队列未满、队列已满（4 种策略）、关闭各阶段提交、计数守恒 |
| `bench.py` | 基准：10 万次提交、队列容量 100，输出耗时与拒绝率分布 |
| `demo.py` | 场景演示：直接打印每种情形下的提交结果与核算 |

## 运行命令

```bash
cd threadpool_reject
python3 test_threadpool.py   # 自测（unittest，12 个用例）
python3 bench.py             # 基准（10 万提交 / 容量 100 / 4 worker）
python3 demo.py              # 场景演示与核算打印
```

## 核心设计：拒绝绝不静默

`submit()` 不抛异常、不静默丢弃，而是**每次都返回一个 `SubmitResult`**：

- `accepted=True`：附 `future`（可取结果/异常）；`caller_runs` 策略下 `executed_by_caller=True`。
- `accepted=False`：附 `reason`（`RejectReason`）与触发它的 `policy`。

被异步丢弃的任务（`discard_oldest` 挤掉的队首、`shutdown_now()` 清空的队列），
其调用方通过自己手里的 `Future.result()` 收到 `RejectedExecution(reason=...)`，
同样显式可达，不存在"任务消失了但没人知道"的路径。

## 拒绝策略（队列满时）

| 策略 | 行为 | 新任务的提交结果 | 被影响方的通知 | 计数影响 |
|---|---|---|---|---|
| `ABORT`（直接拒绝） | 新任务不入队 | `accepted=False, reason=queue_full` | 调用方当场拿到 `SubmitResult` | `rejected+1` |
| `CALLER_RUNS`（调用者执行） | 由调用线程同步执行任务 | `accepted=True, executed_by_caller=True` | 调用方同步拿到 `Future`（已完成） | `completed+1, caller_runs+1` |
| `DISCARD_OLDEST`（丢弃最旧） | 挤掉队首最旧任务，新任务入队 | `accepted=True` | 被挤掉任务的 `Future` 抛 `RejectedExecution(discarded_by_policy)` | `rejected+1`（记在被挤掉者头上） |
| `DISCARD`（丢弃新任务） | 新任务不入队 | `accepted=False, reason=queue_full` | 调用方当场拿到 `SubmitResult` | `rejected+1` |

`ABORT` 与 `DISCARD` 的计数与返回完全一致；区别在于语义（`policy` 字段会如实记录），
`ABORT` 语义上是"应当告警的背压信号"，`DISCARD` 语义上是"可容忍的丢任务"。

## 关闭各阶段的提交结果

状态机：`RUNNING → SHUTTING_DOWN → STOP → TERMINATED`

| 阶段 | 含义 | 此时 submit() 的结果 |
|---|---|---|
| `RUNNING` | 正常服务 | 按队列是否满走正常流程/策略 |
| `SHUTTING_DOWN` | `shutdown()` 已调用，队列继续排空 | `accepted=False, reason=shutting_down` |
| `STOP` | 队列已排空或 `shutdown_now()`，worker 退出中 | `accepted=False, reason=shutdown` |
| `TERMINATED` | 全部 worker 已退出 | `accepted=False, reason=terminated` |

另外：`shutdown()` 后排空期间，已在队列中的任务**保证执行完**；
`shutdown_now()` 会把队列中未执行的任务全部显式拒绝（计数入 `rejected`，
每个任务的 `Future` 抛 `RejectedExecution(shutdown)`），并返回被拒绝的任务数。

## 计数守恒

任意时刻（含运行中快照）满足：

```
submitted == completed + rejected + running + queued
```

- `submitted`：所有 `submit()` 调用次数（含被拒绝的）。
- `completed`：执行完毕的任务数（含执行抛异常的、含 caller_runs 执行的）。
- `rejected`：被拒绝/被丢弃的任务数（队列满拒绝、被挤掉、关闭中提交、shutdown_now 清空）。
- `running` / `queued`：正在执行 / 仍在队列中的任务数。

`PoolStats.conserved` 属性实时校验该等式。基准结束后 `running=queued=0`，
等式退化为 `submitted == completed + rejected`。

## 基准数据（10 万次提交，队列容量 100，4 worker，任务 ~50µs 忙等）

测试机：Linux，Python 3.12。拒绝率按每 1 万次提交一个窗口统计
（取自池内权威 `rejected` 计数器，因此包含 `discard_oldest` 的异步挤掉）。

| 策略 | 提交循环耗时 | 吞吐 | completed | rejected | 守恒校验 |
|---|---|---|---|---|---|
| `abort` | 15.3s | ~6,500 submits/s | 83,993 | 16,007 | 100,000 = 83,993 + 16,007 ✓ |
| `discard` | 13.4s | ~7,500 submits/s | 72,934 | 27,066 | 100,000 = 72,934 + 27,066 ✓ |
| `discard_oldest` | 12.5s | ~8,000 submits/s | 68,188 | 31,812 | 100,000 = 68,188 + 31,812 ✓ |
| `caller_runs` | 15.1s | ~6,600 submits/s | 100,000 | 0 | 100,000 = 100,000 + 0 ✓ |

拒绝率分布（每窗口 %，10 个窗口）：

```
abort          : 61.81  0.00  9.92 37.85  0.00  0.00  0.00  0.00 50.49  0.00   (mean 16.01%)
discard        : 60.18 56.66  0.00 54.54 38.72  0.00  0.00  0.00 60.56  0.00   (mean 27.07%)
discard_oldest : 79.35 38.94  5.59  0.00 43.69 39.50 41.55 25.89 42.50  1.11   (mean 31.81%)
caller_runs    : 全 0（拒绝数恒为 0；其中 11,700 次由调用线程亲自执行）
```

观察与解读：

- 拒绝率呈**突发分布**（0% 与 40~80% 交替），原因是 CPython 的 GIL 按时间片
  在 worker 间切换，worker 成批排空队列后会出现一段"全接受"窗口——这是真实
  行为而非统计错误；多次运行绝对值会波动，守恒等式始终成立。
- `caller_runs` 拒绝率恒为 0：过载被转化为调用方降速（自然背压），
  11,700 次任务由提交线程自己跑完，全部计入 `completed`。
- `discard_oldest` 的 31,812 次拒绝全部是被挤掉的队首任务，其调用方通过
  各自 `Future` 上的 `RejectedExecution(discarded_by_policy)` 得到通知。

## 自测覆盖

`test_threadpool.py`（12 个用例，全部通过）：

- 队列未满：接受、结果正确、任务异常透传且仍计 completed。
- 队列已满：4 种策略各自的返回、reason、调用线程执行、被挤掉者 Future 通知。
- 关闭中提交：`SHUTTING_DOWN` / `STOP` / `TERMINATED` 三阶段分别拒绝且 reason 正确；
  `shutdown()` 后排空保证、`shutdown_now()` 清空计数与通知。
- 计数守恒：200 次混合负载后 `submitted == completed + rejected`，
  且与调用方拿到的 `SubmitResult` 逐条对账一致。

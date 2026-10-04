# 长轮询重连状态机与退避规则

## 1. 状态

| 状态 | 含义 |
|---|---|
| `IDLE` | 空闲，可发起长轮询 |
| `CONNECTING` | 请求已发出，等待成功 / 超时 / 服务端拒绝 |
| `WAITING` | 失败后退避窗口内，禁止发起请求 |
| `GIVEN_UP` | 连续失败达到上界，停止一切调度 |

## 2. 状态迁移表

| # | 当前状态 | 事件 | 条件 | 动作 | 下一状态 |
|---|---|---|---|---|---|
| 1 | IDLE | `start_poll()` | — | 记录 deadline = mono_now + request_timeout | CONNECTING |
| 2 | CONNECTING | 时钟推进 | mono_now ≥ deadline | failures+1，计算退避，next_due = mono_now + delay | WAITING |
| 3 | CONNECTING | `on_failure(TIMEOUT)` | 显式上报超时 | 同 #2 | WAITING |
| 4 | CONNECTING | `on_failure(REJECTED, hint)` | 服务端拒绝 | failures+1，delay 取 hint 优先规则 | WAITING |
| 5 | CONNECTING | 任意失败 | failures ≥ max_attempts | 停止调度，清空 next_due | GIVEN_UP |
| 6 | CONNECTING | `on_success()` | — | failures=0，清空全部计时字段 | IDLE |
| 7 | WAITING | 时钟推进 | mono_now ≥ next_due | 允许 `start_poll()` | CONNECTING |
| 8 | WAITING | `start_poll()` | mono_now < next_due | 抛错，禁止提前重连 | WAITING（不变） |
| 9 | GIVEN_UP | `reset()` | 仅显式调用 | failures=0，清空计时字段 | IDLE |
| 10 | GIVEN_UP | `start_poll()` | — | 抛错，不自愈 | GIVEN_UP（不变） |
| 11 | 任意 | `reset()` | — | 全部归零 | IDLE |

非法迁移（如 CONNECTING 之外的 `on_success`/`on_failure`）一律抛 `RuntimeError`。

## 3. 超时判定

- 单次请求超时：`mono_now ≥ 发起时刻 + request_timeout`（默认 30s）。
- `mono_now` 是注入时钟的**单调视图**：每次读数若小于上次读数，增量按 0 计。
  因此时钟回拨（NTP 校时、人工改时间）不会让超时提前触发，也不会让
  退避窗口提前结束或重复触发；回拨期间流逝的墙上时间被丢弃。

## 4. 退避曲线（Equal Jitter）

```
raw(n)    = base_delay * factor ** (n - 1)        # n = 连续失败次数，从 1 起
capped(n) = min(raw(n), max_delay)
delay(n)  = capped(n)/2 + u * capped(n)/2          # u ∈ [0,1)，来自注入的随机源
```

默认参数 `base=1s, factor=2, cap=60s` 下确定性区间（秒）：

| n | 1 | 2 | 3 | 4 | 5 | 6 | ≥7 |
|---|---|---|---|---|---|---|---|
| raw | 1 | 2 | 4 | 8 | 16 | 32 | 64→60 |
| 区间 | [0.5, 1] | [1, 2] | [2, 4] | [4, 8] | [8, 16] | [16, 32] | [30, 60] |

- 抖动可注入：任何带 `random()` 的对象。测试用 `random.Random(seed)`，
  同一种子下 `u` 序列逐位一致（种子 20261005 的前 12 个值见
  `tests/test_reconnect.py` 的 `EXPECTED_RAW_RANDOM`），
  完整重连时刻表见 `data/backoff_sequence.json` / `.csv`。
- 给定虚拟时钟 + 种子后，每次重连时刻完全确定，测试可精确断言。

## 5. 服务端重试提示（Retry-After）

| 情形 | 取舍 |
|---|---|
| hint ≥ 0 | **优先采用** `min(hint, max_delay)`，忽略本地抖动退避 |
| hint > max_delay | 钳制到 max_delay（客户端上限是硬安全线，防服务端异常值击穿） |
| hint < 0 或缺失 | 回退到本地 Equal Jitter 退避 |
| 任意 hint | 失败计数照常 +1：提示只改等待时长，不改“连续失败”事实，照样计入放弃上界 |

## 6. 上界与恢复

- **连续失败上界**：`failures ≥ max_attempts`（默认 12）→ `GIVEN_UP`，
  不再调度任何重连，只能 `reset()` 显式恢复（防止故障期无限打满对端）。
- **最大退避上限**：`max_delay`（默认 60s）同时约束本地退避与服务端提示。
- **恢复条件**：任意一次 `on_success()` 立即清零连续失败计数，
  后续失败从 n=1 的退避重新开始；`GIVEN_UP` 只能由 `reset()` 解除。

## 7. 时钟回拨

- 库内部只依赖单调视图，回拨不改变 `failures`、不清空退避窗口、
  不提前触发超时或重连（见 `tests/test_reconnect.py::TestClockRollback`）。
- 注意：回拨发生在两次读数之间时，期间正向流逝的时间不可见，
  会被一并丢弃——这是单调语义的必然结果，与真实 monotonic clock 一致。

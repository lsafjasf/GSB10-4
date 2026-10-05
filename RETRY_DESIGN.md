# 事务重试：死锁退避设计说明

Python 3 标准库实现。时间完全由调用方注入（`clock` / `sleep` / `rng`），
测试与基准不等待真实时钟，抖动用带种子的 `random.Random`，序列可复现。

## 1. 冲突分类规则

原则：**只重试“换个时间点重放整个事务可能成功”的瞬时竞争**；
确定性失败（重放必然同样失败）一律快速失败，不浪费预算。

| 类别 | `ConflictKind` | 是否重试 | 样例信号 |
|---|---|---|---|
| 死锁 | `DEADLOCK` | 可重试 | MySQL 1213、PG `40P01 deadlock detected`、ORA-00060 |
| 锁等待超时 | `LOCK_WAIT_TIMEOUT` | 可重试 | MySQL 1205、PG `55P03 lock_not_available`、`innodb_lock_wait_timeout` |
| 可序列化失败 | `SERIALIZATION_FAILURE` | 可重试 | PG `40001 serialization_failure`、SSI 读写依赖中止 |
| 约束冲突 | `CONSTRAINT_VIOLATION` | 直接失败 | PG `23505 unique_violation`、MySQL 1062 重复键、FK `23503` |
| 权限不足 | `PERMISSION_DENIED` | 直接失败 | PG `42501 insufficient_privilege`、MySQL 1142 |
| SQL 语法错误 | `SYNTAX_ERROR` | 直接失败 | PG `42601 syntax_error`、MySQL 1064 |

- 重试要求事务**幂等且整体重放**（应用侧保证）；约束冲突不是“别人放锁就能好”，
  重试只会把同一重复写再撞一遍。
- 分类通过 `TxnConflict(kind, ...)` 显式标注；接入真实数据库时把厂商错误码
  映射到上表即可（错误码映射不在库内硬编码）。

## 2. 退避策略：指数上限 + 全抖动（Full Jitter）

第 `i` 次尝试（0 起）失败后：

```
cap   = min(max_delay, base_delay * multiplier**i)     # 指数上限
delay = rng.uniform(0, cap)                            # 全抖动
sleep(delay)
```

默认 `base_delay=10ms, multiplier=2, max_delay=1s`。
全抖动让同一时刻被死锁回滚的一大批事务不会在相同间隔后“齐刷刷撞回来”。

**可复现的重试时刻序列**（`seed=7`，连续 3 次冲突后成功，虚拟时钟）：

| 第几次重试 | 指数上限 cap | 抖动后延迟 | 实际重试时刻（累计） |
|---|---|---|---|
| 1 | 0.010 s | 0.003238 s | 0.003238 s |
| 2 | 0.020 s | 0.003017 s | 0.006255 s |
| 3 | 0.040 s | 0.026037 s | 0.032293 s |

终态 `success`，共 4 次尝试。同种子任何机器上序列逐位相同；换种子序列不同
（`test_same_seed_same_schedule` 断言）。

## 3. 共享重试预算（总时长不失控）

预算是**每个事务一次、跨所有重试共享**的墙钟预算（默认 5 s），
不是每轮重试单独给一份。每次准备睡眠前检查：

```
if (clock() - started_at) + delay > budget:  -> 终态 EXHAUSTED_BUDGET（不睡这一觉）
```

因此 `已消耗退避时间` 恒满足 `<= budget`，多轮重试不会让总时长滚雪球。

预算消耗样例（`base_delay=0.05, budget=0.20, seed=11`，持续死锁）：

| 睡眠 | 延迟 | 累计消耗 |
|---|---|---|
| 1 | 0.022619 s | 0.022619 s |
| 2 | 0.055977 s | 0.078596 s |
| 下一项（0.10 量级）会使累计 > 0.20 | — | 停止 |

终态 `exhausted_budget`，3 次尝试，消耗 0.078596 s，`last_error` 保留为最后一次
`DEADLOCK`。尝试次数上限（默认 8 次）与预算上限是**两个独立闸口**，先到先生效。

## 4. 终态与最后失败原因

| `Status` | 含义 | `last_error` |
|---|---|---|
| `SUCCESS` | 成功，`value` 为返回值 | None |
| `EXHAUSTED_ATTEMPTS` | 持续冲突达 `max_attempts` 上限 | 最后一次冲突 |
| `EXHAUSTED_BUDGET` | 共享预算耗尽，停止前不再睡眠 | 最后一次冲突 |
| `NON_RETRYABLE` | 不可重试错误，立即快速失败 | 该致命错误 |

所有失败终态都保留**最后一次** `TxnConflict`（含 `kind`、消息、原始 `cause`），
上层可据此打错误日志或转人工。

## 5. 10 万事务基准（`python3 bench.py`）

虚拟时钟（不计睡眠墙钟时间），固定种子的混合负载：
90% 首次成功 / 6% 一次冲突 / 2.5% 两次 / 1% 三次 / 0.4% 持续冲突 / 0.1% 致命错误。
本机一次运行：

```
transactions:             100,000
wall scheduling time:     0.5060 s          # 纯调度（分类+抖动+预算检查）
  per transaction:         5.060 us
total attempts:           116,897
  per attempt:             4.329 us

retry-count distribution:
  retries=0               89,994   89.9940%
  retries=1                 6,081    6.0810%
  retries=2                 2,535    2.5350%
  retries=3                   996    0.9960%
  retries=7 (attempt cap)     394    0.3940%

terminal status:
  success                 99,511   99.5110%
  exhausted_attempts         394    0.3940%
  non_retryable               95    0.0950%

virtual backoff consumed: 347.131 s（跨事务求和）
  per txn: mean=3.47 ms  p50=0.00 ms  max=1.187 s
  budget=5.0 s; any overrun: False                 # 无一事务超预算
```

数字随机器略有波动；分布由种子决定，可复现。

## 6. 边界用例

- 首次成功：1 次尝试、0 次重试、0 退避耗时（`FirstSuccessTest`）。
- 单次冲突：2 次尝试，延迟 ∈ [0, base_delay)，时刻序列逐位复现。
- 连续冲突：多类可重试错误（死锁/锁超时/序列化）混合重试，时刻单调递增。
- 持续冲突：达到 `max_attempts` → `EXHAUSTED_ATTEMPTS`，保留最后原因。
- 不可重试：唯一约束/权限/语法 → `NON_RETRYABLE`，0 退避；可重试之后再遇致命
  错误同样立即终止，原因记录为致命错误。
- 预算优先：预算小于延迟总和时先触发 `EXHAUSTED_BUDGET`；50 个不同种子断言
  总耗时恒 `<= budget`。
- 抖动为 0 合法：`rng.uniform(0, cap)` 可能取到 0，立即重试，调度仍正确。
- 同种子同序列、异种子异序列（复现性双向断言）。

## 7. 运行命令

```bash
python3 test_txn_retry.py   # 12 个自测用例
python3 bench.py            # 100,000 事务基准
```

文件：

- `txn_retry.py` — 库：分类、策略、`Retryer`、终态与结果记录
- `test_txn_retry.py` — 注入虚拟时钟/RNG 的自测
- `bench.py` — 10 万事务分布与调度耗时基准

# 死信归类与重投规则说明

库：`dlq_demo/dlq.py`（Python 3，仅标准库；时间通过 `Clock` 注入，测试完全控制时钟）。

## 1. 失败原因归类（先归类，后计数）

归类发生在任何计数决策之前：不可重试的失败永远不会消耗重试预算，可重试的失败永远不会被提前丢进死信。

| 错误码 | 类别 | 理由 |
|---|---|---|
| `timeout` | 可重试 | 瞬时超时，稍后可能成功 |
| `connection_reset` | 可重试 | 连接被重置，重建即可 |
| `unavailable` | 可重试 | 下游暂时不可用 |
| `rate_limited` | 可重试 | 限流，退避后放行 |
| `internal` | 可重试 | 下游内部错误，可能自愈 |
| `validation` | 不可重试 | 消息体不合法，重投必然再失败 |
| `deserialization` | 不可重试 | 反序列化失败，毒消息 |
| `auth` | 不可重试 | 权限不足，重投无意义 |
| `not_found` | 不可重试 | 目标不存在 |
| `unknown` | 不可重试 | 无法归类的错误按毒消息处理 |
| **未列出的新错误码** | **不可重试（fail-safe 默认）** | 未知错误绝不无限循环；可用 `RetryPolicy.category_overrides` 显式改判 |

## 2. 重投上限

- 可重试失败：最多 `max_attempts` 次投递尝试（默认 **5**）。第 5 次仍失败 → 死信。
- 不可重试失败：恰好 **1** 次尝试，立即死信，不占用重试预算。
- 死信重投回原队列：每条消息最多 `max_requeues` 次（默认 **1**）。重投后 `attempts` 归零重新计，失败历史完整保留。

## 3. 退避与抖动（可复现）

第 `attempt` 次失败后的重投延迟：

```
capped = min(max_delay, base_delay * multiplier^(attempt-1))   # 默认 min(60, 1*2^(n-1))
delay  = U(0, capped)        # full jitter，jitter=False 时 delay = capped
```

- 抖动随机数发生器按消息播种：`seed = f"{policy.seed}|{message_id}|{attempt}"`。
- 因此同一消息的重投时刻序列**与处理顺序、并发交错无关，完全可复现**；
  不同消息之间抖动相互独立，避免重投风暴同步。
- 序列与对拍数据见 `dlq_demo/data/retry_schedule.json`（库输出 vs 独立 oracle 重放，逐点相等）。

## 4. 死信表

每条死信保留：原始消息（id / 原队列 / payload 字节 / headers）+ 全部失败记录
（attempt、错误码、类别、详情、失败时刻）+ attempts/requeues 计数。
`requeue(message_id)` 把消息投回**原队列**，attempts 归零、历史保留、requeues+1。
对拍数据见 `dlq_demo/data/requeue_roundtrip.json`。

## 5. 计数上界论证（重复失败 / 重复投递不失控）

设 `A = max_attempts`，`R = max_requeues`：

1. **attempts ≤ A**：`attempts` 只在 `due_ids()` 把消息从 PENDING 转为 IN_FLIGHT 时 +1；
   每次 IN_FLIGHT 只有一个结局（ack / fail）。可重试失败在 `attempts == A` 时立即转 DEAD，
   DEAD 后不再进入调度堆，故 attempts 永不超 A。
2. **requeues ≤ R**：`requeue()` 在 `requeues >= R` 时拒绝并保持死信。
3. **失败记录数 ≤ A·(R+1)**：每个周期至多 A 次尝试，每次尝试至多 1 条失败记录
   （`fail()` 以 `(attempt, code)` 去重，同一尝试的重复上报是幂等 no-op；
   尝试结束后状态已迁移，迟到的重复 nack 同样幂等）；周期数 ≤ R+1。
4. **重复投递不放大**：`enqueue()` 对重复 message id 直接拒绝（返回 False），
   不重置计数、不复制状态。
5. **总投递次数 ≤ A·(R+1)**，为常数上界，与失败上报次数、投递重复次数无关。

以上上界由 `test_counter_upper_bounds_under_repeated_failure_and_dupes` 用
远超上限的重复失败/重复重投序列实际验证。

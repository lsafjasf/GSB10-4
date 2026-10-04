# 幂等键规则说明

## 1. 键的构造

- **客户端生成**，每个"逻辑操作"一个键（如下单前生成，重试时原样携带）。
- 格式：`^[A-Za-z0-9_-]{8,128}$`。推荐 UUIDv4 / ULID，或 `业务前缀_随机串`
  （如 `ord_9f2c7e1a-0001`）。格式不合法 → **400 `IDEMPOTENCY_KEY_INVALID`**，
  且在任何状态变更之前拒绝。
- 客户端**不得**在键里编码用户 ID、接口名等业务信息——归属判定完全在服务端做，
  防止跨用户伪造。

## 2. 作用域（防互相覆盖）

存储记录的地址是**三元组复合键**，不是客户端键本身：

```
scoped_key = (user_id, endpoint, key)
```

- `user_id`：已认证主体（来自会话/令牌，不来自请求体）。
- `endpoint`：`"METHOD /path"`，如 `"POST /orders"`。
- 三元组直接作为 dict 的 tuple 键，**结构上不可被字符串拼接扁平化**：
  `("ab","c")` 与 `("a","bc")` 永不碰撞（有测试覆盖）。
- 因此：同一键串，不同用户 → 各自独立首次执行；不同接口 → 各自独立首次执行。

## 3. 请求指纹（冲突检测）

```
fingerprint = SHA256( "v1\n" + METHOD + "\n" + path + "\n" + SHA256(body) )
```

同一 scoped key 再次出现时，指纹不同 → **409 `IDEMPOTENCY_KEY_CONFLICT`**
（键冲突，客户端 bug，不会执行也不会复用旧结果）。

## 4. 判定矩阵

| 已有记录状态 | 指纹 | 判定 | 响应 |
|---|---|---|---|
| 无 | — | 首次，执行 handler | handler 的原始响应 |
| in_progress | 相同 | 并发重复（见 §5 策略） | WAIT / 202 / 409 |
| in_progress | 不同 | 键冲突 | 409 `IDEMPOTENCY_KEY_CONFLICT` |
| completed（未过期） | 相同 | 重放 | 与首次**完全相同**的状态码+正文 |
| completed（未过期） | 不同 | 键冲突 | 409 `IDEMPOTENCY_KEY_CONFLICT` |
| completed（已过期）→ expired | 任意 | 键过期 | 410 `IDEMPOTENCY_KEY_EXPIRED` |
| expired | 任意 | 键过期（终态） | 410 `IDEMPOTENCY_KEY_EXPIRED` |

键格式不合法在任何查表之前 → 400。

## 5. 首次请求未结束时的并发重复（三选一，默认 WAIT）

- **WAIT（默认）**：重复方阻塞等待首个请求完成，随后返回与首个请求
  **完全一致**的响应。带 `wait_timeout_seconds`（默认 30s），超时返回
  409 `IDEMPOTENCY_WAIT_TIMEOUT`。
- **REJECT**：立即 409 `IDEMPOTENCY_REQUEST_IN_PROGRESS`，让客户端稍后重试。
- **IN_PROGRESS**：立即 202 Accepted + 处理中正文；客户端轮询，完成后
  同一键重放得到真实结果。

若首个请求的 handler **抛异常**：记录被驱逐（不缓存 5xx 崩溃），
等待中的重复请求会被唤醒并把该键当作**新的首次请求**重新执行——
客户端重试永远安全，不会卡在幻影 in_progress 上。

## 6. 结果复用一致性

- 重放返回的 `status` 与 `body` 与首次响应**逐字节相同**（对拍数据见
  `golden/golden.jsonl`，`01_first_order` vs `02_duplicate_order`、
  `04_other_user_same_key` vs `07_other_user_replay` 的
  `status`/`response_body_hex` 完全相等）。
- 重放仅额外加响应头 `Idempotency-Replayed: true`（头部标记，不进正文）。

## 7. 过期 vs 冲突（严格区分）

- 过期判定是**惰性**的：访问时发现 `now >= expires_at`，记录转入
  `expired` 终态并释放响应体内存；`sweep()` 可周期性物理清除。
- **过期优先于冲突**：过期键即使用不同 body 重放也是 410 而非 409。
- **过期是终态**：过期后重放永远 410，绝不重新执行 handler
  （防止"过期后重放被当成首次请求"造成重复下单）。
- 过期 ≠ 冲突：410 表示"这个键的生命周期已结束"；409 表示"键被错误复用"。

## 8. 错误码一览

| HTTP | code | 含义 |
|---|---|---|
| 400 | `IDEMPOTENCY_KEY_INVALID` | 键格式非法 |
| 409 | `IDEMPOTENCY_KEY_CONFLICT` | 同键不同请求 |
| 409 | `IDEMPOTENCY_REQUEST_IN_PROGRESS` | 处理中（REJECT 策略） |
| 409 | `IDEMPOTENCY_WAIT_TIMEOUT` | 等待首个请求超时 |
| 410 | `IDEMPOTENCY_KEY_EXPIRED` | 键已过期（终态） |
| 202 | — | 处理中（IN_PROGRESS 策略） |

## 9. 性能与内存上界（实测，100 万次请求）

环境：Python 3.12，16 核，Linux。基准：`bench/benchmark.py`。

**判定耗时**（每次 `execute` 的判定开销，不含业务 handler）：

| 路径 | mean | p50 | p99 | 吞吐 |
|---|---|---|---|---|
| 首次（建记录） | 5.6 µs | 1.9 µs | 12.1 µs | ~175,000 req/s |
| 重放（查表复用） | 1.2 µs | 0.8 µs | 2.3 µs | ~860,000 req/s |

**存储内存上界**（100 万条 completed 记录常驻）：

| 口径 | 总量 | 每条 |
|---|---|---|
| Python 对象（tracemalloc） | 1,295 MiB | ~1,358 B |
| 进程 RSS 增量（含解释器开销/碎片，保守上界） | 2,370 MiB | ~2,485 B |

上界公式（每条记录）：

```
M_record ≈ 1.3 KB + len(key) + len(fingerprint=64) + len(body) + len(headers)
M_total  ≤ N × M_record × 1.9   (RSS 口径保守系数)
```

基准参数：key 24 字符、body 26 字节。100 万键 → Python 对象 ≤ 1.3 GiB，
RSS 口径 ≤ 2.4 GiB。生产环境应配 `sweep()` 周期清理过期记录，
常驻记录数 = 吞吐 × TTL，内存随 TTL 线性伸缩。

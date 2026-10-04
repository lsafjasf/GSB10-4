# 幂等键设计与校验（Python 3，仅标准库）

## 1. 键规则

### 1.1 键的构造（作用域隔离）

```
key = BLAKE2b-128( len(user_id)  || user_id
                 || len(endpoint) || endpoint
                 || len(client_key) || client_key )
```

- `user_id`：用户隔离。不同用户即使携带相同的客户端键，存储键也不同，**绝不互相覆盖**。
- `endpoint`：接口隔离（如 `POST /orders` 与 `POST /refunds`），不同接口**绝不互相覆盖**。
- `client_key`：客户端提供的幂等键（建议 UUIDv4）。每段都做 **4 字节长度前缀**，
  杜绝 `("ab","c")` 与 `("a","bc")` 拼接歧义。
- 输出 16 字节（128 bit）。100 万个键的生日碰撞概率 ≈ N²/2^129 ≈ **1.5×10⁻²⁷**。

### 1.2 请求指纹（冲突判定）

```
fingerprint = BLAKE2b-128(canonical(body))
```

同一个键但正文指纹不同 → **冲突 422**，不是重试（防止 A 的键被 B 的正文复用）。
JSON 用 `sort_keys + 紧凑分隔符` 规范化后再哈希；原始字节用 `fingerprint_payload`。

### 1.3 记录生命周期（三态）

```
(none) ──begin()──▶ IN_PROGRESS ──complete(status,body)──▶ COMPLETED
                     │   ▲                                      │
                  abort() │ 等待(wait_timeout)            ttl 到期（默认24h）
                     ▼   │                                      ▼
                   (none) ◀────────────────  EXPIRED（墓碑：留指纹，删正文）
                                          tombstone_ttl（默认+7天）后彻底遗忘
```

### 1.4 判定结果与 HTTP 映射

| `begin()` 返回 | 含义 | `handle_once` 响应 |
|---|---|---|
| `EXECUTE` | 首次（或墓碑已彻底过期） | 执行业务，存入真实 status/body |
| `REPLAY` | 已有完成结果 | **原样返回存储的 status 与 body（逐字节一致）** |
| `IN_PROGRESS` | 首个请求尚未结束 | 默认阻塞等待其结束后回放；超过 `wait_timeout` → **409** |
| `CONFLICT` | 同键不同正文指纹 | **422** |
| `EXPIRED` | 键已过期但仍在墓碑窗内 | **410**，绝不执行业务 |

失败也要 `complete(500, body)`：重试会回放同一次失败，而不是再打一次业务。
只有"什么都没发生"（如崩在副作用之前）才用 `abort()` 释放槽位。

**过期 ≠ 冲突 ≠ 首次**：TTL 到期后正文删除、转墓碑；墓碑窗内任何重放都得到 410，
业务函数 0 次调用；过期与正文不符同时成立时，过期优先（仍是 410）；
只有超过 `ttl + tombstone_ttl` 才彻底遗忘，之后才视为全新首次请求。

## 2. 对拍数据（`python3 test_idempotency.py` 实测输出）

同键重复：业务只执行 1 次，两次响应逐字节相同。

```
[对拍] first : 201 b'{"attempt": 1, "order_id": "ORD-0001"}'
[对拍] replay1: 201 b'{"attempt": 1, "order_id": "ORD-0001"}' identical: True
[对拍] replay2: 201 b'{"attempt": 1, "order_id": "ORD-0001"}' identical: True
```

首个请求在途时并发重复：第二个请求阻塞等待，拿到首个请求的同一份响应。

```
[对拍-并发] first : (200, b'{"order_id":"ORD-0001","attempt":1}')
[对拍-并发] second: (200, b'{"order_id":"ORD-0001","attempt":1}') identical: True
```

过期后重放：业务不再执行，返回 410。

```
[对拍-过期] first : 201 b'{"attempt": 1, "order_id": "ORD-0001"}'
[对拍-过期] replay: 410 b'{"error":"idempotency_key_expired"}' (handler calls: 1)
```

## 3. 耗时与内存上界（100 万请求，实测）

环境：Python 3.12，本机单次实测；正文均为互不相同的 64 字节。

### 3.1 判定耗时

| 路径 | 总耗时 | 平均 | p50 | p99 | 吞吐 |
|---|---:|---:|---:|---:|---:|
| 首次 `begin+complete` | 1.391 s | 1391 ns | 1110 ns | 2960 ns | 7.2×10⁵/s |
| 重复 `begin`→REPLAY | 0.789 s | 789 ns | 540 ns | 1490 ns | 1.27×10⁶/s |

并发等待路径与 REPLAY 同构（条件变量唤醒后多一次 `wait`），等首个请求 `complete` 即返回。

### 3.2 存储内存上界（实测 RSS 增量）

| 状态 | 每键 | 100 万键 |
|---|---:|---:|
| COMPLETED（含 64 B 正文） | **396 B/键** | **378 MB** |
| EXPIRED 墓碑（只留 16 B 指纹） | **315 B/键** | **301 MB** |

解析上界（正文按 B 字节计，留 20% 分配器余量）：

```
COMPLETED ≤ 1.2 × (80 记录 + 49 键 + 49 指纹 + (33+B) 正文 + 24 到期时间 + ~60 dict摊销)
          ≈ 1.2 × (295 + B) 字节/键      → B=64 时 ≈ 431 B/键（实测 396，未超）
墓碑       ≈ 1.2 × (80 + 49 + 49 + 48 两个float + ~60) ≈ 343 B/键（实测 315，未超）
```

按上限保守取值：**完成态 ≤ 450 B/键（正文≤64B）→ 100 万键 ≤ 450 MB；
墓碑 ≤ 350 B/键 → ≤ 350 MB**。正文越大按 `+B` 线性增长。
`purge_past_tombstones()` 定期清掉超过墓碑窗的记录回收内存（默认 7 天 > 客户端重试窗口）。

## 4. 边界用例（`test_idempotency.py`，12 项全过）

1. 首次请求执行一次；2. 重复请求回放逐字节一致；3. 并发重复等待并回放同一结果；
4. 并发重复等不到首个请求 → 409，业务仍只执行 1 次；5. 同键不同正文 → 422；
6. 过期重放 → 410，**不按首次请求处理**；7. 过期 + 正文不符 → 仍 410（过期优先）；
8. 超过 ttl+tombstone_ttl 后键彻底遗忘，可重新首次下单；9. 不同用户/不同接口作用域隔离；
10. 持错指纹 `complete()` 被拒；11. `abort()` 后重试可执行；12. 500 失败响应也原样回放。

## 5. 运行命令

```bash
cd idempotency
python3 test_idempotency.py -v          # 自测 + 对拍输出
python3 benchmark.py                    # 100 万次判定耗时
python3 benchmark.py --rss completed    # 100 万完成态记录内存
python3 benchmark.py --rss tombstone    # 100 万墓碑内存
```

## 6. 接入示例

```python
from idempotency import IdempotencyStore, make_key, fingerprint_json, handle_once

store = IdempotencyStore(ttl=24*3600, tombstone_ttl=7*24*3600, wait_timeout=2.0)

def create_order_response(user_id, body):
    key = make_key(user_id, "POST /orders", body["idempotency_key"])
    fp  = fingerprint_json(body["payload"])
    return handle_once(store, key, fp, lambda: do_create_order(body))
```

注意：单机内存实现按本题要求给出上界；多机部署时把同一 `dict` 换成
带 TTL+墓碑的共享存储（Redis：`SET NX` 占位 + 完成后 SET 结果，EXPIRE 分两段），
状态机与判定规则不变。

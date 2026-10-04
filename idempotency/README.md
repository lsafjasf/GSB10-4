# idempotency — 幂等键库（Python 3 标准库，零依赖）

客户端重试场景下的服务端幂等判定：同一 `(用户, 接口, 键)` 的重复请求
复用首次结果，键冲突、键过期、并发在途分别给出明确响应。

## 目录

```
idempotency.py        库本体（存储、判定、并发策略、过期）
server_helpers.py     HTTP 集成小层（异常 -> 状态码/错误体、重放头）
tests/                28 个自测（首次/重复/并发重复/过期/冲突/作用域隔离）
golden/               对拍数据：golden.jsonl + 生成/校验脚本
bench/                100 万次请求耗时与内存基准
examples/             http.server 集成演示
docs/KEY_RULES.md     键构造、作用域、判定矩阵、错误码、性能上界
```

## 运行

```bash
cd idempotency

# 自测
python3 -m unittest discover -s tests -v

# 对拍：生成 + 校验（含 first==replay 逐字节相等断言、二次生成确定性校验）
python3 golden/golden_data.py generate
python3 golden/golden_data.py verify

# 基准：100 万次请求的判定耗时与内存上界
python3 bench/benchmark.py

# 集成演示
python3 examples/http_demo.py &
curl -i -X POST localhost:8000/orders \
     -H 'Idempotency-Key: ord_demo_00001' -H 'X-User-Id: user-A' \
     -d '{"sku":"A-1","qty":2}'
# 再发一次同样的请求 -> 正文逐字节相同，多一个 Idempotency-Replayed: true 头
```

## 用法

```python
from idempotency import IdempotencyStore, Response, request_fingerprint

store = IdempotencyStore(ttl_seconds=24*3600)   # 默认 WAIT 策略

resp, replayed = store.execute(
    user_id="user-A",                 # 来自认证，不来自请求体
    endpoint="POST /orders",
    key="ord_9f2c7e1a-0001",          # 客户端 Idempotency-Key 头
    fingerprint=request_fingerprint("POST", "/orders", body_bytes),
    handler=lambda: Response(201, b'{"order_id":"O-1"}'),
)
# replayed=True 时 resp 与首次响应逐字节相同，handler 不会再次执行
```

规则细节（键格式、作用域、判定矩阵、过期/冲突区分、性能上界）见
`docs/KEY_RULES.md`。

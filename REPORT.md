# 批量接口逐项重试 — 交付报告

语言：Python 3（仅标准库）。代码在 `src/`，自测 12 项全部通过。

## 运行命令

```bash
cd src
python3 test_batch_retry.py -v        # 自测：12 个用例（全成功/全失败/部分失败/重试再失败/超时对账/分类/对拍）
python3 diff_check.py 42 1000         # 对拍：批量+重试 vs 逐条提交，不一致则退出码非 0
python3 benchmark.py 100000 ../out/status_table_100k.csv   # 10 万条压测 + 导出完整逐项状态表
```

## 设计要点

- `submit_batch_with_retry(client, items, max_retries)`：首轮整批提交；之后每一轮
  只把**当前状态为 FAILED_RETRYABLE** 的条目组成新批次重发。SUCCESS 与
  FAILED_NON_RETRYABLE 条目一旦确定就永不再发，成功项副作用不可能重复
  （模拟服务端对“已应用条目被重发”直接抛 AssertionError，压测 10 万条 duplicates=0）。
- 重试轮本身若发生整批超时/连接重置（`BatchTransportError`，部分条目可能已在服务端
  生效），客户端**逐条调用 `query` 对账**：已应用→SUCCESS，永久错误→
  FAILED_NON_RETRYABLE，未应用→FAILED_RETRYABLE（进入下一轮）。因此任何条目
  最终必然落在三种已判定状态之一，不存在 UNKNOWN。

## 错误分类（分类依据）

**依据：同一个请求原样再发一次是否可能成功。** 传输层/服务端临时故障可能成功 →
可重试；由条目内容或权限导致的失败，原样重发必然同样失败 → 不可重试（重试只会
放大负载）。未识别的新错误码按不可重试处理（安全兜底，避免无限重试）。

| 类别 | 错误码 | 样例 |
|---|---|---|
| 可重试 | `TIMEOUT` | 请求超时，服务端可能已应用 |
| 可重试 | `CONNECTION_RESET` | TCP 连接被重置 |
| 可重试 | `RATE_LIMIT` | HTTP 429 限流，退避后可成功 |
| 可重试 | `SERVER_5XX` | HTTP 500/502/503/504 服务端临时故障 |
| 可重试 | `UNAVAILABLE` | 服务维护中 / 熔断打开 |
| 不可重试 | `VALIDATION` | HTTP 400/422 参数或业务校验失败 |
| 不可重试 | `AUTH` | HTTP 401/403 凭证错误 / 无权限 |
| 不可重试 | `NOT_FOUND` | HTTP 404 引用的资源不存在 |
| 不可重试 | `CONFLICT` | HTTP 409 版本冲突 / 前置条件失败 |
| 不可重试 | `DUPLICATE` | 幂等键已存在且载荷不同 |
| 不可重试 | `PAYLOAD_TOO_LARGE` | HTTP 413 条目超限 |

## 逐项状态表（部分失败场景实测输出）

场景：10 条；2、3 首次可重试失败后重试成功；4 不可重试失败；5 持续可重试失败直至
预算耗尽（max_retries=3）。

```
item_id   status                 attempts retries last_error   decided_by
it-0000   SUCCESS                1        0       -            submit
it-0001   SUCCESS                1        0       -            submit
it-0002   SUCCESS                2        1       -            submit
it-0003   SUCCESS                2        1       -            submit
it-0004   FAILED_NON_RETRYABLE   1        0       CONFLICT     submit
it-0005   FAILED_RETRYABLE       4        3       SERVER_5XX   submit
it-0006   SUCCESS                1        0       -            submit
it-0007   SUCCESS                1        0       -            submit
it-0008   SUCCESS                1        0       -            submit
it-0009   SUCCESS                1        0       -            submit
side_effects: it-0000,0001,0002,0003,0006,0007,0008,0009 （各恰好 1 次）
```

10 万条完整状态表：`out/status_table_100k.csv`（由 benchmark 导出）。

## 对拍数据（批量+重试 vs 逐条单独提交）

`python3 diff_check.py 42 1000` 实测输出（另在 unittest 中对 seed=1,7,42,2026
各 300 条做了同样断言）：

```
seed=42 n=1000 max_retries=8
batch path : rounds=7    side_effects=845
single path: rounds=1549 side_effects=845
status+attempts mismatches: 0
side-effect ledgers equal (as sets w/ multiplicity): True
no duplicated side effects: True
DIFFERENTIAL RESULT: IDENTICAL
```

两条路径使用同一随机种子生成的完全相同的失败脚本；最终每条目的状态、尝试次数、
副作用账本（含多重集）完全一致——批量+逐项重试与逐条单独提交语义等价。

## 10 万条耗时与重试次数分布

分布设定（seed=20261005）：88% 首次成功；8% 重试 1 次成功；2% 重试 2 次成功；
1% 永远可重试失败；1% 不可重试失败。并在第 2 轮提交注入一次整批超时以验证对账
路径（该轮 8058 条经 reconcile 判定）。max_retries=4。

```
items=100000  max_retries=4  submit_rounds=5
wall_time=0.194~0.233s  (约 0.002 ms/item，纯内存模拟服务端)
item-submissions total=116140  resubmit_rate=16.14%
final status:
  SUCCESS                98058
  FAILED_RETRYABLE        996   （重试预算耗尽，状态已判定）
  FAILED_NON_RETRYABLE    946
retries-per-item distribution:
  k=0  88897  88.897%   cumulative 88.897%
  k=1   8058   8.058%   cumulative 96.955%
  k=2   2049   2.049%   cumulative 99.004%
  k=3      0   0.000%   cumulative 99.004%
  k=4    996   0.996%   cumulative 100.000%
decided_by: submit=91942, reconcile=8058
side_effects=98058 unique=98058 duplicates=0
```

## 自测覆盖（12 项，全部通过）

- 分类：5 个可重试样例、6 个不可重试样例、未知错误码安全兜底
- 全部成功（1 轮，50 条副作用各 1 次）
- 全部失败-不可重试（0 次重试）/ 全部失败-可重试（预算耗尽，状态可判定）
- 部分失败（只重发失败项；成功项 attempts=1；副作用无重复）
- 重试中再次失败→最终成功；重试中再次失败→最终失败（均带 last_error）
- 整批超时→逐条对账，无 UNKNOWN，已应用条目不重复应用
- 500 条随机混沌脚本下副作用零重复
- 对拍：4 个种子 × 300 条，批量 vs 逐条结果完全一致

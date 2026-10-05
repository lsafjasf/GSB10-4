# 异步重试：抖动 + 并发去重（Python 3，仅标准库）

## 文件
- `async_retry.py` — 库：`RetryExecutor`（重试+去重）、`full_jitter_delay`、`Outcome`、`RetryExhausted`、`DedupStats`
- `test_async_retry.py` — 8 个自测（时间/随机源全部注入）
- `bench_retry.py` — 抖动分布数据 + 100 万次重试基准
- `bench_output.txt` — 基准完整输出

## 运行命令
```bash
python3 -m unittest test_async_retry -v   # 自测
python3 bench_retry.py                    # 分布数据 + 基准
```

## 抖动分布（Full Jitter）
`delay = rand() * min(cap, base * 2**attempt)`，`rand() ∈ [0,1)`，base=0.1s，cap=5s。
- 上下界：`[0, min(cap, base·2^attempt))`，上限取不到（`random()` 严格小于 1）
- 期望值：`E[delay] = limit / 2`（均匀分布均值）
- 10 万样本直方图（10 桶近似均匀，每桶 ≈10000）：见 `bench_output.txt` 第 1 节
  - attempt=0: limit=0.1s, E=0.05s, 实测均值 0.050014s
  - attempt=2: limit=0.4s, E=0.2s, 实测均值 0.200058s
  - attempt=6: 被 cap 截断 limit=5s, E=2.5s, 实测均值 2.500721s
- 边界用例：`rand→0` 得 delay=0.0；`rand→1-2⁻⁵³` 得 delay 无限逼近但严格小于 limit；`base·2^attempt > cap` 时恒被 cap 截断（`test_jitter_boundary_values`）

## 可复现性
固定种子 `seed=42`，两次运行逐位一致（`identical=True`），序列见 `bench_output.txt` 第 2 节。

## 并发去重（single-flight）
同一 `key` 的并发 `execute()` 共享一个在途 `asyncio.Task`；后到者拿到
`Outcome(source="deduplicated")`，`stats.merged` 计数。
- 1000 并发同 key：fn 实际执行 1 次，merged=999（见 `bench_output.txt` 第 4 节）
- 在途任务结束后同 key 再次调用会重新执行（`test_consecutive_batches_run_separately`）
- 等待者用 `asyncio.shield` 隔离取消，不会误杀共享任务

## 结果区分（不混返回）
- 成功且真实执行 → `Outcome(source="executed")`
- 成功且去重命中 → `Outcome(source="deduplicated")`
- 重试耗尽 → 抛 `RetryExhausted`（携带 `attempts`、`last_error`、`key`），
  共享任务耗尽时全部等待者收到同一异常（`test_dedup_propagates_exhaustion_distinctly`）

## 100 万次重试基准（时间注入，零真实等待）
- 1,000,000 次抖动计算+sleep 注入：22.9 s（≈43,700 retries/s）
- 内存峰值：tracemalloc Python 对象 0.28 MiB；进程 RSS 28.5 MiB（前后无增长，无累积）
- 若真实等待，这些抖动相当于 50,001.5 s 挂钟时间——注入时钟将其压缩为纯逻辑开销

## 测试覆盖
| 情形 | 测试 |
|---|---|
| 无并发重试成功 | `test_no_concurrency_success_after_retries` |
| 高并发去重计数 | `test_high_concurrency_dedup_counted`（1000 并发） |
| 抖动边界值 | `test_jitter_boundary_values`、`test_bounds_and_capping` |
| 重试耗尽 | `test_retry_exhausted_distinct_from_outcome` |
| 种子可复现 | `test_seed_reproducible_sequence` |
| 批次间重新执行 | `test_consecutive_batches_run_separately` |
| 共享任务耗尽传播 | `test_dedup_propagates_exhaustion_distinctly` |

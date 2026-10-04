# 10 万请求调度耗时基准

负载由注入的虚拟时钟驱动，墙钟只用于度量**调度器自身**的开销；下游耗时/排队/backoff 均不真实睡眠。固定随机种子，可复现。

- 请求数：100000；段数：4（权重 (2, 2, 1, 1)）；每段最多 3/3/8 次尝试；backoff 1 tick
- 下游尝试耗时：3–45 tick 均匀分布；排队：0/1/2/5 tick 加权抽样
- Python：3.12.3；平台：Linux x86_64（16 核）

| 总预算 | 每段最大尝试 | 墙钟总耗时 | 每请求 | 每次派发决策 | 派发尝试总数 | budget_exhausted 比例 | 其他结果 |
|---:|---:|---:|---:|---:|---:|---:|---|
| 100 tick | 3 | 1.625 s | 16.25 µs | 3.603 µs | 450941 | 13.11% | success 41.64%, stage_error 0.00%, attempts_exhausted 45.26% |
| 50 tick | 3 | 1.674 s | 16.74 µs | 4.212 µs | 397377 | 6.71% | success 2.02%, stage_error 0.00%, attempts_exhausted 91.27% |
| 100 tick | 8 | 2.078 s | 20.78 µs | 3.738 µs | 555893 | 49.99% | success 41.66%, stage_error 0.00%, attempts_exhausted 8.36% |

不变量校验：全部 300000 次运行 `used <= total` 且 `remaining >= 0`，违规 0 次。

## 纯切片决策微基准

`Budget.slice_for(2, 6)` 连续调用 400000 次：0.0617 s，即每次 154 ns（无下游、无重试，仅算预算算术）。

复现：`python3 tools/benchmark_100k.py`（原始数据：`data/benchmark_100k.json`）。

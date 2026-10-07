# 度量口径（Metrics Specification）

分析器 `fairloop.analyzer.analyze(records)` 是纯函数：同一份运行记录
（`RunRecord` 序列）在任何机器、任何时刻重放分析，结果逐字节一致。
分析过程不读真实时钟、不用随机数、不依赖字典遍历顺序。

## 一条记录

```
RunRecord(reg_id, name, site, ready_ts, start_ts, end_ts)
```

- `reg_id`：注册号（每次 `schedule` 递增；周期任务重注册会换新号）
- `name` / `site`：回调名 / 注册点（`文件:行号` 或显式标签），定位“是谁干的”
- `ready_ts`：入队（就绪）时刻
- `[start_ts, end_ts)`：实际执行窗口

## 拖延度量（不报“平均值”了事）

| 指标 | 定义 |
|---|---|
| 单次占用时长 `duration` | `end_ts - start_ts`，全量分布：min / p50 / p90 / p99 / max / mean |
| 就绪延迟 `delay` | `start_ts - ready_ts`，同样给出完整分布 |
| 分位数口径 | 最近秩（nearest-rank），无插值，确定性 |
| 卡住次数 `stalled_runs` | `delay > stall_threshold` 的执行次数（阈值由调用方注入） |
| 利用率 `utilization` | 执行窗口并集长度 / 墙钟长度；`idle = wall - busy` 即空闲时间片 |

## 拖累归因（谁干的）

回调 B 的等待窗口为 `(ready_B, start_B]`。凡与该窗口重叠的执行窗口 A，
按**重叠时长**计入 A 的诱导等待（induced wait）：

```
induced(A, B) = | [start_A, end_A) ∩ (ready_B, start_B] |
```

- B 的等待由它等待期间**实际占用循环的每个回调**按占用比例分摊；
- 等待窗口内的空闲段不计入任何人；
- 零时长窗口不拖累任何人；
- 按 `(name, site)` 聚合（周期任务重注册共享同一身份），
  输出 `total_induced_wait`、`victims`（被拖累的执行次数）、
  `max_occupied` 等，按 `total_induced_wait` 降序、
  `max_occupied` 降序、`(name, site)` 升序排列 —— 完全确定。

## 复杂度与开销上界

- 记录器（monitor）：每次回调执行 O(1) —— 两次时钟读数 + 一次
  `list.append`，不排序不聚合；内存随执行次数线性增长。
- 分析器：O(n log n)（排序 + 等待事件扫描线 + 差分数组），
  与回调数量线性对数关系，无平方项。
- 实测（本仓库 `scripts/benchmark.py`，16 核容器，Python 3.12）：
  - 监控开销：每事件额外约 280–300ns，总倍率约 2.9x（20 万次回调，
    5 次取最小值；基线为纯循环调度本身，故倍率含调度器固定成本）；
  - 100 万回调：分析约 2.1–2.2s，内存峰值约 248MiB（tracemalloc，
    含 100 万条轨迹本身）。

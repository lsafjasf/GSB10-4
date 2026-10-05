# 可更新键的索引优先队列

## 源码

- `indexed_priority_queue.py`：`IndexedMinHeap` 库实现，仅使用 Python 标准库。
- `self_test.py`：逐步路径、不变量、相等键、只增/只减、极端值和异常边界自测。
- `differential_test.py`：与“每次操作后重建堆”的朴素实现对拍。
- `benchmark.py`：100 万次操作耗时与内存基准。

## 更新路径

堆内部维护三张同步表：

- `_heap`：按二叉堆层序保存元素。
- `_positions[item]`：元素当前所在的堆下标，定位元素是 O(1)。
- `_keys[item]`：元素当前键。

`update_key(item, new_key)` 的路径：

1. 通过 `_positions[item]` O(1) 找到当前下标；不存在则抛 `KeyError`。
2. `new_key < old_key`：当前元素只可能破坏与父节点的堆序，因此从当前位置 `_sift_up` 向上调整。
3. `new_key > old_key`：当前元素只可能破坏与子节点的堆序，因此从当前位置 `_sift_down` 向下调整。
4. `new_key == old_key`：任何父子边都不会因此失效，不交换、不改位置，路径为 `none`。

相等键使用插入序号作为次序的第二分量，逻辑比较键为 `(key, insertion_rank)`。这只用于确定稳定的全序，不会把相等键元素合并，也不会让同值更新产生丢失或重复。

复杂度：

- 定位元素：O(1)
- 更新键：O(log n)
- 插入/删除最小值：O(log n)
- 额外空间：O(n)

## 不变量

`check_invariants()` 每次校验：

- `_heap`、`_positions`、`_keys`、`_insertion_rank` 大小一致。
- 每个元素在堆中只出现一次。
- 每个元素的位置映射等于其实际堆下标。
- 每个元素都有键和插入序号。
- 对每个子节点 `c` 及父节点 `p=(c-1)//2`，都满足父节点不大于子节点。

逐步更新校验摘要（完整逐条输出在 `invariant_report.txt`）：

| 操作 | 起始下标 | 路径 | 交换 | 结束下标 | 校验 |
|---|---:|---|---|---:|---|
| `D: 12 -> 1` | 7 | 向上 | `7->3, 3->1, 1->0` | 0 | OK |
| `A: 5 -> 15` | 3 | 向下 | `3->7` | 7 | OK |
| `B: 9 -> 9` | 4 | 不调整 | 无 | 4 | OK |
| `H: 6 -> -10^12` | 3 | 向上 | `3->1, 1->0` | 0 | OK |
| `E: 3 -> 10^12` | 3 | 向下 | `3->7` | 7 | OK |

其他自测结果：

- 初始 8 次插入后均通过不变量校验。
- 4 个键全相等的元素执行同值更新后，位置集合不变，元素无丢失、无重复。
- 只减键：64/64 次更新后不变量通过。
- 只增键：64/64 次更新后不变量通过。
- `-inf`、`inf`、缺失元素、重复元素、NaN 边界均通过。
- 300 步随机自测中，每步后不变量和外部镜像均一致。

## 对拍数据

朴素参照实现位于 `differential_test.py` 的 `NaiveRebuildHeap`：每次插入、更新或弹出后都重新 `heapify`。

固定种子 `20261005` 的 1000 步对拍结果（完整数据在 `data/differential_summary.json` 和 `data/differential_trace.csv`）：

| 指标 | 结果 |
|---|---:|
| 总操作 | 1000 |
| 插入 | 410 |
| 更新 | 444 |
| 弹出 | 146 |
| 同值更新 | 51 |
| 每步最小值 | 与朴素实现一致 |
| 最终完整弹出序列 | 与朴素实现一致 |
| 结果 | PASS |

## 100 万次操作基准

环境：Python 3.12.3，Linux WSL2，AMD Ryzen 7 9700X。完整 JSON 在 `data/` 下。

混合负载（85% 更新、7.5% 插入、7.5% 弹出，初始 10000 元素）：

| 指标 | 结果 |
|---|---:|
| 操作数 | 1,000,000 |
| 更新 / 插入 / 弹出 | 849,336 / 75,153 / 75,511 |
| 上浮更新 / 下沉更新 | 459,296 / 390,040 |
| 操作耗时 | 1.900841 s |
| 吞吐 | 526,082.90 ops/s |
| 普通运行 RSS 峰值 | 20.773 MiB |
| Python 分配峰值（tracemalloc） | 4.466 MiB |
| 结束后不变量 | PASS |

纯更新负载（100 万次 `update_key`，初始 1,000,001 元素）：

| 指标 | 结果 |
|---|---:|
| 操作数 | 1,000,000 |
| 上浮更新 / 下沉更新 | 499,469 / 500,531 |
| 建堆耗时 | 1.390900 s |
| 更新耗时 | 4.209426 s |
| 吞吐 | 237,562.08 updates/s |
| 普通运行 RSS 峰值 | 414.832 MiB |
| Python 分配峰值（tracemalloc） | 371.595 MiB |
| 结束后不变量 | PASS |

说明：`tracemalloc` 会引入额外跟踪开销；因此耗时以未开启 `--memory` 的运行为准，Python 分配峰值以开启 `--memory` 的运行为准。

## 运行命令

```bash
# 自测并生成逐步不变量报告
python3 self_test.py --output invariant_report.txt

# 与每次重建堆的朴素实现对拍
python3 differential_test.py \
  --operations 1000 \
  --seed 20261005 \
  --trace data/differential_trace.csv \
  --summary data/differential_summary.json

# 百万混合操作：真实耗时
python3 benchmark.py \
  --operations 1000000 \
  --workload mixed \
  --seed 340 \
  --output data/benchmark_mixed_timing.json

# 百万混合操作：Python 分配峰值
python3 benchmark.py \
  --operations 1000000 \
  --workload mixed \
  --seed 340 \
  --memory \
  --output data/benchmark_mixed_memory.json

# 百万纯更新：真实耗时
python3 benchmark.py \
  --operations 1000000 \
  --workload update-only \
  --seed 341 \
  --output data/benchmark_update_only_timing.json

# 百万纯更新：Python 分配峰值
python3 benchmark.py \
  --operations 1000000 \
  --workload update-only \
  --seed 341 \
  --memory \
  --output data/benchmark_update_only_memory.json
```

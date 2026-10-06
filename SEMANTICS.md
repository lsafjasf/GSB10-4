# 窗口函数帧边界计算 — 语义说明与对拍数据

实现：`window_frame.py`（Python 3，仅标准库）。
自测：`test_window_frame.py`（18 个用例，全部通过）。
基准：`bench_window_frame.py`（100 万行耗时/内存/一致性）。

## 1. 帧边界语义

- **分区与排序**：行先按 `partition_key` 分区，分区内按 `(order_key, 原始下标)` 升序排列。
  原始下标是**决胜键**（tie-breaker）：排序键重复时，行的物理顺序由输入下标唯一确定，
  因此排序键不稳定（有重复值）时结果依然可复现（见 §5）。
- **区间开闭**：帧边界一律为**闭区间** `[start, end]`（分区内位置，含两端）。
  `start > end` 表示**空帧**（不裁剪、不报错，是合法结果）。
- **三种帧模式**（边界差一个元素整列就偏，核心区别就在这里）：

| 模式 | 偏移单位 | 并列值（peer）处理 | 越界处理 |
|---|---|---|---|
| `ROWS` | 物理行数 | **截断**：只数行，同值的行可以落在帧外 | 裁剪到分区内（可能因此产生空帧） |
| `RANGE` | 排序键的**值** | **并入**：与当前行同值的行整体进/出帧，同键各行帧完全相同 | 由键值范围自然决定（可能空帧） |
| `GROUPS` | peer 组数 | **并入**：同组行同帧 | 组号裁剪到分区内（可能空帧） |

- **边界类型**（start/end 均可用，如 `BETWEEN 2 PRECEDING AND 1 PRECEDING`）：
  - `UNBOUNDED`：start 侧 = 分区第一行，end 侧 = 分区最后一行；
  - `CURRENT_ROW`：ROWS 下 = 当前行位置；RANGE/GROUPS 下 = 当前行所在 peer 组的首/末行；
  - `n PRECEDING` / `n FOLLOWING`：ROWS 按行数 ±n；RANGE 按键值 ∓n 用二分定位
    （start = 第一个 `>= key-n` 的位置，end = 最后一个 `<= key+n` 的位置）；GROUPS 按组号 ±n。
- **默认帧**（不显式给出时）：`RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`，与 SQL 一致。
- **空帧取值**（确定值，SQL 标准）：`count = 0`；`sum / avg / min / max / first_value / last_value = None`。
- **全帧**（`UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING`）：每行的帧都是整个分区，
  聚合结果对分区内每行相同（确定值）。

## 2. 手工推算逐行对拍数据

数据表 B（单分区，排序键 k 含并列，v 为聚合值；§2.2–§2.6 均用此表）：

| 行号 | k | v |
|---|---|---|
| 0 | 1 | 10 |
| 1 | 2 | 20 |
| 2 | 2 | 25 |
| 3 | 3 | 30 |
| 4 | 5 | 40 |

### 2.1 ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING（按行，并列被截断）

下表数值用数据表 A `v = [10, 20, 20, 30, 40]`（无额外排序键，按输入位置）：

| 行号 | 帧 [start,end] | sum | count | min | max | first_value | last_value |
|---|---|---|---|---|---|---|---|
| 0 | [0,1] | 30 | 2 | 10 | 20 | 10 | 20 |
| 1 | [0,2] | 50 | 3 | 10 | 20 | 10 | 20 |
| 2 | [1,3] | 70 | 3 | 20 | 30 | 20 | 30 |
| 3 | [2,4] | 90 | 3 | 20 | 40 | 20 | 40 |
| 4 | [3,4] | 70 | 2 | 30 | 40 | 30 | 40 |

该结果与 `test_a_rows_1p_1f` 一致；表 B 上同一帧的逐行结果见 §2.2 对照。

### 2.2 并列值对照：ROWS（截断） vs RANGE（并入），帧 = UNBOUNDED PRECEDING..CURRENT ROW，sum

| 行号 | k | v | ROWS 帧 | ROWS sum | RANGE 帧 | RANGE sum |
|---|---|---|---|---|---|---|
| 0 | 1 | 10 | [0,0] | 10 | [0,0] | 10 |
| 1 | 2 | 20 | [0,1] | 30 | [0,2] | 55 |
| 2 | 2 | 25 | [0,2] | 55 | [0,2] | 55 |
| 3 | 3 | 30 | [0,3] | 85 | [0,3] | 85 |
| 4 | 5 | 40 | [0,4] | 125 | [0,4] | 125 |

k=2 的两行：ROWS 下帧不同（30 vs 55，并列被截断）；RANGE 下帧完全相同（55，并列并入）。
对应 `test_b_rows_vs_range_tie_contrast`。

### 2.3 RANGE BETWEEN 1 PRECEDING AND CURRENT ROW（按值，并列并入）

| 行号 | k | 键值范围 [k-1, k] | 帧 [start,end] | sum | count |
|---|---|---|---|---|---|
| 0 | 1 | [0,1] | [0,0] | 10 | 1 |
| 1 | 2 | [1,2] | [0,2] | 55 | 3 |
| 2 | 2 | [1,2] | [0,2] | 55 | 3 |
| 3 | 3 | [2,3] | [1,3] | 75 | 3 |
| 4 | 5 | [4,5] | [4,4] | 40 | 1 |

### 2.4 空帧：RANGE BETWEEN 1 PRECEDING AND 1 PRECEDING

| 行号 | k | 键值范围 [k-1, k-1] | 帧 | sum | count |
|---|---|---|---|---|---|
| 0 | 1 | [0,0] | [0,-1] **空帧** | None | 0 |
| 1 | 2 | [1,1] | [0,0] | 10 | 1 |
| 2 | 2 | [1,1] | [0,0] | 10 | 1 |
| 3 | 3 | [2,2] | [1,2] | 45 | 2 |
| 4 | 5 | [4,4] | [4,3] **空帧** | None | 0 |

### 2.5 全帧：ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING

每行帧都是 [0,4]：sum=125，count=5，min=10，max=40，avg=25.0（5 行完全相同）。

### 2.6 GROUPS BETWEEN 1 PRECEDING AND 1 FOLLOWING（组：{1} {2,2} {3} {5}）

| 行号 | 组号 | 帧 | sum |
|---|---|---|---|
| 0 | 0 | [0,2] | 55 |
| 1 | 1 | [0,3] | 85 |
| 2 | 1 | [0,3] | 85 |
| 3 | 2 | [1,4] | 115 |
| 4 | 3 | [3,4] | 70 |

### 2.7 单行 / 多分区

- 单行表 `[99]`：任意非空帧 sum=99、count=1；`ROWS 1 PRECEDING..1 PRECEDING` 为空帧 → sum=None、count=0。
- 多分区（输入乱序）：`[("b",3,300),("a",1,10),("b",1,100),("a",2,20),("b",3,30)]`，
  默认帧 sum 按输入顺序返回 `[430, 10, 100, 30, 430]`
  （a 区累计 10、30；b 区累计 100，k=3 并列并入得 430）。

以上每张表都与 `test_window_frame.py` 中的断言一一对应；另有 200 组固定种子随机用例
与朴素实现 `evaluate_naive`（逐行收集帧内元素再聚合）逐元素对拍。

## 3. 边界用例清单（均已覆盖）

- 单行表：空帧 / 非空帧（`test_single_row`）
- 全表一帧：`UNBOUNDED PRECEDING..UNBOUNDED FOLLOWING`（`test_full_frame`）
- 并列值跨边界：k=2 两行在 ROWS 下被截断、RANGE/GROUPS 下并入（§2.2、§2.3、§2.6）
- 空帧：ROWS 与 RANGE 两种产生方式（§2.4、`test_a_rows_empty_frame`）
- 帧越界裁剪：`test_frame_bounds_empty_and_clip`
- 多分区 + 输入乱序：`test_multi_partition`
- 非法帧：负偏移、未知模式、GROUPS 非整数偏移、未知聚合（`test_bad_frame`）

## 4. 100 万行性能数据（实测）

环境：Python 3.12.3，Linux x86-64。数据：100 万行 `(分区键∈[0,100), 排序键∈[0,10000), 值)`，
排序键大量重复（不稳定）。耗时为 3 次运行最优值；内存为 `tracemalloc` 增量峰值
（计算过程中新分配的 Python 对象峰值，不含输入数据本身）。

| 帧 / 聚合 | 耗时 (s) | 增量峰值 (MB) | 吞吐 (万行/s) |
|---|---|---|---|
| ROWS UNBOUNDED PRECEDING..CURRENT ROW, sum | 5.53 | 114.4 | 18.1 |
| ROWS 100 PRECEDING..100 FOLLOWING, min | 7.28 | 114.4 | 13.7 |
| RANGE 1000 PRECEDING..CURRENT ROW, sum | 7.94 | 114.4 | 12.6 |
| RANGE UNBOUNDED PRECEDING..CURRENT ROW（默认帧）, sum | 6.54 | 114.4 | 15.3 |
| GROUPS 5 PRECEDING..5 FOLLOWING, sum | 11.30 | 114.4 | 8.9 |
| ROWS UNBOUNDED PRECEDING..UNBOUNDED FOLLOWING（全帧）, avg | 7.22 | 114.4 | 13.8 |

进程 RSS 高水位（含输入数据与全部历史分配）：520.1 MB。
完整输出见 `bench_result.txt`，可用 `python3 bench_window_frame.py` 复现。

## 5. 可复现性

- **机制**：排序键 = `(分区键, 排序键, 原始下标)`，原始下标作决胜键 + Python 稳定排序，
  排序键任意重复时同一输入的物理行序唯一确定。
- **重复运行一致性**（100 万行，同一输入连跑 3 次，结果 SHA-256 前 16 位）：

| 帧 | 哈希 | 3 次运行 |
|---|---|---|
| ROWS UNBOUNDED PRECEDING..CURRENT ROW, sum | 90b98f288c34a001 | 一致 |
| ROWS 100 PRECEDING..100 FOLLOWING, min | d6a98486a91c3937 | 一致 |
| RANGE 1000 PRECEDING..CURRENT ROW, sum | ce7a80116836946c | 一致 |
| RANGE UNBOUNDED PRECEDING..CURRENT ROW, sum | 264331a6c09b615a | 一致 |
| GROUPS 5 PRECEDING..5 FOLLOWING, sum | 377acddb2113ae73 | 一致 |
| ROWS UNBOUNDED..UNBOUNDED（全帧）, avg | 5adc06131bbed9ad | 一致 |

- **输入打乱**：RANGE/GROUPS 的结果多重集与打乱前完全一致（帧只取决于键值，
  `test_range_shuffle_invariant`）；ROWS 依赖物理顺序，不保证跨打乱一致，
  但同一输入重复运行结果一致（`test_rows_shuffle_deterministic_per_input`）。

## 6. 运行命令

```bash
python3 test_window_frame.py -v   # 自测（18 个用例，含全部手算对拍表）
python3 bench_window_frame.py     # 100 万行基准（可用参数指定行数，如 100000）
```

库用法：

```python
from window_frame import Frame, PRECEDING, CURRENT_ROW, evaluate

rows = [(1, 10), (2, 20), (2, 25), (3, 30), (5, 40)]   # (排序键, 值)
frame = Frame("RANGE", (PRECEDING, 1), (CURRENT_ROW, None))
evaluate(rows, frame, "sum", order_key=lambda r: r[0], value=lambda r: r[1])
# -> [10, 55, 55, 75, 40]
```

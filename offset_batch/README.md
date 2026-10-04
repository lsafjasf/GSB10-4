# 消费位点批量提交与回退（Python 3，仅标准库）

解决的问题：位点批量提交只能推进**连续前缀**；中间有空洞时，后面位点已完成
也不能把高水位提过去，否则重启会跳过空洞、丢消息。

## 文件

| 文件 | 作用 |
| --- | --- |
| `offset_commit.py` | 库：`OffsetTable`（内存位点表）、`run_batch`（批量处理+回退）、`WalCommitter`（WAL 持久化） |
| `test_offset_commit.py` | 自测：严格递增、乱序、空洞、回退、WAL、对拍、SIGKILL 崩溃恢复 |
| `crash_child.py` / `crash_demo.py` | 崩溃点实验被管子进程 / 多崩溃点数据采集 |
| `bench.py` | 100 万条消息耗时与内存基准 |
| `diff_test_data.json` | 对拍明细（400 组随机序列 / 50617 步，由测试生成） |
| `crash_results.json` | 5 个崩溃点的恢复结果 |
| `bench_results.json` | 基准原始数据 |

## 位点规则（连续前缀判定）

- 位点是非负整数，严格递增；`base` 表示 `[0, base)` 已连续处理完。
- 完成通知调用 `mark_done(offset)` 进入一张**有序不相交区间表**，相邻区间自动合并；
  重复完成、已提交位点再次完成一律忽略。
- `advance()` 只从 `base` 开始消费连续区间：第一段起点不等于 `base` 就是空洞，
  **立即停在空洞之前**，后面的区间原样保留。

示例（完成序列 `0,1,4,5,8`，空洞 2、3、6、7）：

```
mark 0,1,4,5,8 -> intervals=[[0,1],[4,5],[8,8]]
advance()      -> base=2，可提交位点序列 [0,1]，停在空洞 2
hole()         -> 2
mark 3         -> 仍不能推进（2 缺失）
mark 2         -> 区间合并
advance()      -> base=6（4,5 被前缀带过去）
```

## 失败回退（范围最小）

`run_batch(table, offsets, handler)` 在处理期间**不修改位点表**，成功位点先放在
暂存列表；只有整批成功才并入表并 `advance()`。任意 handler 抛异常即返回：

- `rolled_back`：失败位点之前本批已处理的位点（回退数据，调用方可据此撤销副作用）；
- `failed_at` / `remaining`：失败位点与其后未处理位点；
- 位点表完全不变，之前批次已推进的 `base` 不受影响 —— 回退范围恰好是本批。

## 提交与处理解耦 / 崩溃不丢消息

`WalCommitter` 用追加 WAL（每条记录 fsync），顺序严格为：

1. 业务处理成功；
2. 写 `D <offset>` 并 fsync（完成记录）；
3. `commit()` 推进连续前缀后写 `C <base>` 并 fsync（检查点）。

恢复时重放：最后一条无换行符的半截记录丢弃；`C` 恢复高水位，`D` 中不连续的
部分恢复回区间暂存。因为**先处理后落盘**，崩溃最多造成在途消息重放（at-least-once），
绝不会跳过未处理位点。批量可用 `mark_done_batch()` 合并 fsync。

## 对拍数据（推进结果 == 逐条提交）

参考实现：`done` 集合 + `while base in done` 逐条推进。400 组随机序列（含重复、
空洞、乱序）共 50617 步，每步断言批量表与参考实现的 `base` 完全一致，全部通过；
明细见 `diff_test_data.json`（前 5 组含逐步轨迹）。片段：

```
完成 18,25,9,6,... -> base 始终为 0（0 未完成，不推进）
完成 0            -> base 才开始前进，与逐条提交逐位相同
```

## 崩溃点测试结果（`crash_results.json`，N=2000，SIGKILL）

| 崩溃时最后处理位点 | 恢复起点 base | 重放条数 | 跳过位点 |
| --- | --- | --- | --- |
| 32 | 1 | 32 | 0 |
| 161 | 129 | 33 | 0 |
| 49 | 1 | 49 | 0 |
| 49 | 1 | 49 | 0 |
| 105 | 65 | 41 | 0 |

重放条数都小于检查点窗口（64），`skipped=[]`；子进程重启输出 `start=<base>
final_base=2000`，第三次重启输出 `start=2000 final_base=2000`（无重复处理）。
随机时点的 SIGKILL 恢复由 `TestCrashRecovery` 在每次测试中真实执行。

## 100 万条消息耗时与内存（本机实测，见 `bench_results.json`）

| 场景 | 耗时 | 峰值内存(tracemalloc) | 位点表 |
| --- | --- | --- | --- |
| 严格递增，每条 mark+advance | 1.47 s（1.47 µs/条） | 432 B | 恒定 0~1 区间 |
| 窗口乱序（窗口 1024 洗牌） | 2.10 s（2.10 µs/条） | 约 90 KB | 峰值 286 区间 |
| 100 万条全暂存后一次 advance | 0.68 s | 约 488 B | 连续时塌缩为 1 区间 |
| WAL 组提交（4096 条/fsync） | 2.18 s | 约 282 KB | 日志 8.9 MB |
| WAL 每条 fsync（2000 条采样外推） | 约 1327 s / 100 万 | — | fsync 主导 |

内存上界：位点表空间 = `O(H+1)` 个区间，H 为已完成集合中的洞数。连续流恒定
`O(1)`；最坏形状（完成/空洞交替）实测约 **136 B/区间**，100 万条、50 万个洞的
极端情况下上界约 **68 MB**（见 `sparse_holes` 行，100k 区间采样线性外推）。

## 运行命令

```bash
cd offset_batch
python3 -m unittest test_offset_commit -v   # 全部自测（含 SIGKILL 崩溃恢复、对拍）
python3 crash_demo.py                        # 多崩溃点恢复数据 -> crash_results.json
python3 bench.py                             # 100 万基准 -> bench_results.json
```

要求：Python 3.10+（使用 `bisect` 的比较用法），无第三方依赖。

# 递归查询：环检测 + 深度限制

纯 Python 3 标准库实现，全迭代（BFS / 迭代 Tarjan），深度 1 万的链不爆栈。

## 文件

| 文件 | 说明 |
|---|---|
| `recquery.py` | 库：`build_graph` / `query` / `find_cycles` |
| `cases.py` | 手工分析对拍数据（9 个用例，含期望值） |
| `test_recquery.py` | 自测（对拍 + 边界用例），`python3 -m unittest -v` |
| `run_cases.py` | 生成环报告 `cycle_report.json`（含对拍结论） |
| `benchmark.py` | 性能基准，输出 `benchmark_result.json` |
| `cycle_report.json` | 环报告（实际 vs 手工期望，逐条比对） |
| `benchmark_result.json` | 耗时与内存数据 |

## 运行命令

```bash
python3 -m unittest -v     # 自测（9 个测试）
python3 run_cases.py       # 生成环报告 cycle_report.json
python3 benchmark.py       # 百万边性能基准
```

## 设计

- **环检测**：在"深度限制内可达"的子图上跑迭代 Tarjan 求 SCC。SCC 大小 >1
  或大小为 1 且带自环 ⇒ 该 SCC 全部节点都在环上（数学上严格：节点在环上
  当且仅当处于非平凡 SCC 或有自环）。这给出**环上的全部节点**。
- **环节点序列**：每个 SCC 内枚举基本环（以环上最小节点为起点规范化，
  不重复），给出如 `[a,b,c,a]` 的闭合序列。枚举有双重保险：环数上限
  `cycle_cap`（默认 100）+ 搜索步数预算 `step_budget`（默认 100 万），
  超大 SCC 不会卡死；截断时 `cycles_truncated=true`，但环节点全集仍完整。
- **深度限制**：BFS 按最短深度展开，深度达 `max_depth` 且有出边的节点进入
  `truncated` 列表，不再展开。visited 集合保证重复边/自环/环都不会死循环。
- **两种限制同时生效**，结果形态用 `status` 区分：

| status | 含义 | 形态特征 |
|---|---|---|
| `ok` | 都未触发 | `truncated=[]` 且 `cycles=[]` |
| `depth_limited` | 仅深度截断 | `truncated` 非空，`cycles=[]` |
| `cycle_detected` | 仅检测到环 | `cycles` 非空，`truncated=[]` |
| `cycle_detected_and_depth_limited` | 同时触发 | 两者均非空 |

## 对拍数据（手工分析 vs 实际，全部一致）

`cases.py` 中 9 个用例，期望值为手工推导，`run_cases.py` 实际运行比对，
结果 `all_match=true`（详见 `cycle_report.json`）：

| 用例 | 图 | 手工期望（与实际一致） |
|---|---|---|
| acyclic_deep_chain | 链 1→…→10，depth=3 | `depth_limited`，截断于节点 4，无环 |
| acyclic_chain_full_depth | 同链，depth=20 | `ok`，10 节点全达 |
| single_cycle_with_tail | a→b→c→a，c→d | 环节点 {a,b,c}，序列 [a,b,c,a]，d 不在环上 |
| two_cycles_intersect | 环 a-b-c-a 与 c-d-e-c 交于 c | 同一 SCC 5 节点全在环上，基本环 2 条：[a,b,c,a]、[c,d,e,c] |
| self_loop | x→x，x→y | 环节点 {x}，序列 [x,x]，y 不在环上 |
| duplicate_edges | (1,2)×3，(2,3) | 去重 2 条，`ok`，无环 |
| unreachable_cycle | 环 p↔q 不可达，s→t | `ok`，不报告不可达环 |
| depth_limit_and_cycle_both | 环 2↔3 + 深链，depth=2 | `cycle_detected_and_depth_limited`，截断于 3，环 [2,3,2] |
| two_disjoint_cycles | 环 a↔b、d↔e 经 a→d 相连 | 两个环区域，序列 [a,b,a]、[d,e,d] |

## 边界用例（均有确定结果，不死循环）

- 起点不在图中：`visited={start:0}`，`ok`
- `max_depth=0`：只访问起点，`depth_limited`，看不到环
- 自环：序列为 `[s,s]`；重复自环边去重
- 重复边：`build_graph` 去重并返回 `duplicates_removed` 计数
- 无可达路径 / 不可达的环：不进入结果
- 深度 1 万链：迭代实现，无 RecursionError，全部可达
- 200 节点全互连稠密图：遍历与检测正常终止，枚举被 cap 截断但环节点全集完整

## 性能（100 万条边、深度 1 万）

环境：Python 3.12.3，Linux x86_64。图：1 万深链 + 随机边补足 100 万条
（30 万节点，去重 4 条）。实测见 `benchmark_result.json`：

| 指标 | 数值 |
|---|---|
| 节点 / 边 | 299,601 / 999,996 |
| 建图（含去重排序） | 2.43 s |
| 查询（BFS + Tarjan + 环枚举） | 5.72 s |
| 总耗时（含图生成 1.88 s） | 10.03 s |
| Python 堆峰值（tracemalloc） | 256.9 MB |
| 进程 RSS 峰值（ru_maxrss） | 780.5 MB |

结果形态：检测出 1 个巨型环区域，275,964 个节点在环上，基本环枚举被
步数预算截断（`cycles_truncated=true`），环节点全集完整。

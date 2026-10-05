# 确定性社区发现（纯 Python 标准库）

Louvain 贪心模块度最大化（Blondel et al., 2008），只用 Python 3 标准库。
特点：固定种子可精确复现、孤立点/自环归属确定、社区数自适应、迭代有上界。

## 文件

| 文件 | 说明 |
|---|---|
| `community.py` | 算法与图结构：`Graph`、`detect_communities`、`modularity` |
| `metrics.py` | 质量指标与各社区对比表：`community_report`、`global_metrics`、`pairwise_cut_matrix`、`adjusted_rand_index` |
| `graphs.py` | 确定性测试图/基准图生成器 |
| `selftest.py` | 自测 + 对拍数据生成（断言，失败返回非 0） |
| `benchmark.py` | 10 万点 / 50 万边规模基准（耗时、内存、可复现对拍） |
| `out/selftest_report.txt` | 小图各社区对比表 |
| `out/selftest_data.json` | 对拍数据（多种子、ARI、Q、逐社区划分） |
| `out/benchmark_report.txt` | 10 万点结果的逐社区对比表 |
| `out/benchmark_data.json` | 基准耗时 / 内存 / 对拍数据 |

## 运行命令

```bash
python3 selftest.py       # 自测与对拍，约 1 秒；全过返回 0
python3 benchmark.py      # 10 万点基准，约 2 分钟；重写 out/benchmark_*
```

最小用法：

```python
from community import Graph, detect_communities
from metrics import format_report

g = Graph()
for u, v in [(0, 1), (1, 2), (0, 2), (2, 3), (3, 4), (4, 5), (3, 5)]:
    g.add_edge(u, v)
result = detect_communities(g, seed=42)
print(result["partition"], result["communities"], result["modularity"])
print(format_report(g, result["partition"]))
```

## 边的计数约定（自环一致性）

- 普通无向边权 `w`：`adj[u][v] = adj[v][u] = w`，对总度数贡献 `2w`。
- 自环权 `w`：`adj[u][u] = w`（只存一次），但度数贡献 `2w`（两个端点都在 u）。
- `m2 = Σ degree = 2 × 总边权`（自环与普通边一视同仁）。
- 聚合层内部边自环存 `2w`、度数贡献 `2w`，保证每层 `m2` 守恒。

## 质量度量指标

**模块度 Q（Newman，含自环）**

```
Q = Σ_C [ 2·I(C)/m2 − (T(C)/m2)² ]
```

`I(C)`：两端都在 C 内的边权之和（自环计一次）；`T(C)`：C 内节点度数之和。
一次移动的增益（u 先从当前社区移除）：`ΔQ = (2/m2)·(c_in(u→C) − k_u·T(C)/m2)`。

**coverage（边覆盖率）**：`Σ_C I(C) / 总边权`，即未跨社区的边占比。

**conductance（切导率）**：`φ(C) = cut(C) / min(T(C), m2 − T(C))`，越小越“封
闭”；社区占满全图时为 0。

**density（密度）**：`I(C) / (n_C(n_C−1)/2 + 自环数)`，无向位置占满比例。

**ARI（调整兰德指数）**：两划分的重合度，`1.0` 表示完全一致。用于对拍
（同种子复现、跨种子稳定性、与 planted 真值对比）。

`metrics.format_report` 输出**每个社区一行**的对比数据：大小、内部边权、切
边权、体积、自环数、conductance、密度，并附每对社区之间的跨边权矩阵；示例见
`out/selftest_report.txt`（链状图被切成 10 个连续段，段间跨边各 1，端段
conductance=0.143，内部段=0.25）。

## 可复现性 / 对拍数据

确定性保证：

1. 节点按 id 排序映射成稠密下标，候选社区按 id 升序评估，相同增益时保留最
   小社区 id；
2. 默认 `order="sorted"`：每轮按固定顺序访问，**结果与种子无关**；
3. `order="shuffled"`：每轮用 `random.Random(seed + level)` 洗牌，经典
   Louvain 行为，**相同种子仍给出完全相同的划分**；
4. 输出社区 id 按“社区内最小节点”重新编号为 `0..k−1`（canonical）。

对拍（`out/selftest_data.json`，每个用例跑 seed=42/42/43/2024 四次）：

| 用例 | 社区数 | Q | 同种子两次划分 | sorted 下跨种子 ARI |
|---|---|---|---|---|
| chain_40（链状） | 10 | 0.6690 | 完全一致 | 1.0 |
| 两团 2×30+1 桥 | 2 | 0.4989 | 完全一致 | 1.0 |
| 稠密随机 G(200,0.5) | 7 | 0.0515 | 完全一致 | 1.0 |
| 链 + 5 孤立点 | 10 | 0.3519 | 完全一致 | 1.0 |
| 自环/孤立点 | 3 | 0.2755 | 完全一致 | 1.0 |
| 加权双团弱桥 | 2 | 0.4967 | 完全一致 | 1.0 |

10 万点基准对拍：run1 与 run2（同种子）逐节点划分完全一致，ARI=1.000000；
seed 42 与 seed 999（sorted 顺序）ARI=1.000000。

## 孤立点与自环归属（不随机）

- **纯孤立点**（度 0）：局部阶段直接跳过，永远留在单点社区，与访问顺序、
  种子无关；自测中在 seed=1/100/999 下均保持单点。
- **只有自环的点**：自环不计入对其他社区的 `c_in`（其两端永远随该点移动），
  该点留在单点社区（自环使它天然独立，移动无收益）。
- **既有自环又有普通边的点**：正常按普通边增益移动，自环跟随。
- 全图无边时返回“每点一个社区”，`stop_reason="edgeless_graph"`。
- 边界断言还包括：空图（0 社区）、单点图、单边图（合并为 1 社区，Q=0）。

## 终止条件与迭代上界

社区数不设死值。外层聚合循环（`detect_communities`）终止条件：

1. `no_merge_level`：某一层局部优化没有产生任何合并（正常收敛）；
2. `single_community`：只剩 1 个社区；
3. `edgeless_graph`：无边图直接返回。

上界：每层局部扫描 **max_passes=100** 轮，外层聚合 **max_levels=64** 层
（每层节点数严格减少，因此必然终止；命中 pass 上界的层记录在
`pass_cap_levels`，仍会继续聚合到下一层而不是直接结束）。返回字段
`levels` / `passes_per_level` / `stop_reason` 可审计实际迭代量。

## 10 万点 / 50 万边基准

机器：Python 3.12.3，Linux x86_64（WSL2）。图：40 个 planted 社区
（三角形富集 + 随机边，90% 边在社区内），100 个孤立点，50 个自环，
实际 499,970 条边。

| 指标 | 数值 |
|---|---|
| 建图耗时 | 1.07 s |
| 检测耗时（run2，无插桩） | **16.9 s** |
| 检测耗时（run1，tracemalloc 插桩，仅用于内存测量） | 72.0 s |
| 进程峰值 RSS | **437.5 MB** |
| Python 分配器峰值（tracemalloc） | 136.4 MB |
| 社区数 / Q / coverage | 246 / 0.8440 / 0.8690 |
| 与 planted 真值 ARI | 0.9128（planted 划分自身 Q=0.875） |
| 层级与每层扫描轮数 | 4 层，[17, 18, 3, 1]，正常收敛 |

说明：纯 Python 实现 + `dict` 邻接表，无任何第三方库。tracemalloc 会带来
约 4 倍运行时开销，故以 run2 为准；RSS 为整个进程（含三次检测与建图）峰值。

## 已知局限（如实说明）

- 这是**原始 Louvain**：单节点贪心可停在局部最优，且有模块度分辨率极限；
  基准上它把 40 个 planted 社区细分成 246 个子块（Q 0.844 vs planted
  0.875，ARI 0.91）。需要更强保证可在此基础上加 Leiden 式细化（超出本次
  交付范围）。
- 局部树状（聚类系数≈0）的纯随机稀疏图是单节点贪心的已知劣性局部最优；
  基准图因此按真实网络的做法注入三角形，这一选择写在 `graphs.py` 注释中。
- 图结构假定节点 id 可排序（int/str 等均可），边权为正。

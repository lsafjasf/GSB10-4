# 位图索引编码选择与运算 — 交付报告

Python 3，仅标准库。核心库 `bitmap_codec.py`，自测/对拍 `test_bitmap.py`，
百万位基准 `bench.py`。

## 1. 两种编码

| 编码 | 负载 | 体积 | 适用 |
|---|---|---|---|
| `RAW` | 每 8 位打包 1 字节的紧凑位图 | 恒为 `ceil(n/8)` | 中等密度；可按机器字直接并行交并差 |
| `SPARSE` | 只存“少数位”位置，位置排序后做差分，再用 LEB128 varint 编码 | 与少数位个数和间隔相关 | 极稀疏 / 极稠密 |

`SPARSE` 带 `negated` 标志（记为 `NSPARSE`）：密度 d ≤ 0.5 时存 1 的位置；
d > 0.5 时存 0 的位置并取反。所以稀疏端和稠密端都只付少数位的代价，
全空 / 全满只有 4 字节（1 字节标志 + nbits 的 varint，负载为空）。

序列化格式：`字节0 = encoding<<1 | negated` → varint(nbits) → 负载。

## 2. 选择依据与切换条件

选择只与数据分布挂钩（n 与 1 的个数 popcount），不猜内容：

1. `raw_bytes = ceil(n/8)`。
2. `minority = min(popcount, n-popcount)`，最大间隔上界 `max_gap = n//minority + 1`，
   估算 `sparse_bytes = minority * varint_len(max_gap)`。
3. `sparse_bytes < raw_bytes` 选 `SPARSE/NSPARSE`，否则选 `RAW`；平局选 `RAW`
   （RAW 运算更快）。位置已知时再用精确 varint 体积做一次最终裁决。

对随机分布，间隔 ≈ 1/d，varint 在间隔 <128 时为 1 字节，故分界约在
d ≈ 1/8：d ≤ ~0.1 选 SPARSE，0.2 ≤ d ≤ 0.8 选 RAW，d ≥ ~0.9 选 NSPARSE。
极端两端优势巨大（见下表：125004 B → 4 B，约 3 万倍）。

## 3. 编码选择表与空间占用（n = 1,000,000，随机分布）

| 密度 d | popcount | 选中编码 | RAW 字节 | SPARSE 字节 | 实际占用 | 编码 ms | 解码 ms |
|---:|---:|:---|---:|---:|---:|---:|---:|
| 0.0 | 0 | SPARSE | 125004 | 4 | **4** | 0.01 | 0.11 |
| 1e-6 | 1 | SPARSE | 125004 | 7 | **7** | 0.01 | 0.09 |
| 1e-5 | 10 | SPARSE | 125004 | 32 | **32** | 0.01 | 0.07 |
| 1e-4 | 100 | SPARSE | 125004 | 220 | **220** | 0.03 | 0.09 |
| 1e-3 | 1000 | SPARSE | 125004 | 1894 | **1894** | 0.22 | 0.25 |
| 0.01 | 10000 | SPARSE | 125004 | 12819 | **12819** | 2.19 | 1.82 |
| 0.05 | 50000 | SPARSE | 125004 | 50080 | **50080** | 9.21 | 6.44 |
| 0.1 | 100000 | SPARSE | 125004 | 100005 | **100005** | 21.00 | 15.10 |
| 0.2 | 200000 | RAW | 125004 | 200004 | **125004** | 51.60 | 0.04 |
| 0.5 | 500000 | RAW | 125004 | 500004 | **125004** | 91.70 | 0.05 |
| 0.8 | 800000 | RAW | 125004 | 200004 | **125004** | 167.75 | 0.05 |
| 0.9 | 900000 | NSPARSE | 125004 | 100004 | **100004** | 162.80 | 14.38 |
| 0.99 | 990000 | NSPARSE | 125004 | 12802 | **12802** | 133.79 | 1.86 |
| 0.999 | 999000 | NSPARSE | 1888 | 169.40 | 0.30 |
| 0.9999 | 999900 | NSPARSE | 125004 | 221 | **221** | 144.21 | 0.11 |
| 1.0 | 1000000 | NSPARSE | 125004 | 4 | **4** | 115.79 | 0.07 |

原始数据：`encoding_table.csv`。环境 Python 3.12.3，耗时随机器波动。
稠密端编码耗时偏高（~0.12–0.17 s）是因为基准输入是 100 万个位置的 Python 列表，
构造输入本身耗时；从大整数构造（`Bitmap.from_int`）走字节直转 / 少数位遍历，无此问题。

## 4. 两种编码解码一致性（对拍 A）

对 13 组分布（全空、全满、首/尾单位置、极稀疏 10 位、极稠密缺 10 位、
d∈{0.001,0.01,0.1,0.5,0.9,0.99} 随机、交替位、头/尾连续块）分别强制用
`RAW` 与 `SPARSE` 编码，断言三者相等：`raw.to_int() == sparse.to_int() == 参考位图`。
例（n=100000）：

| case | RAW vs SPARSE | popcount | sha1(位图) 前 12 位 | 结果 |
|---|:---|---:|---|:---|
| empty | RAW vs SPARSE | 0 | 5ba93c9db0cf | PASS |
| full | RAW vs SPARSE | 100000 | 81d86f185401 | PASS |
| single_first / single_last | RAW vs SPARSE | 1 | bf45… / 36aa… | PASS |

## 5. 交并差混用对拍（对拍 B）

运算时两个操作数编码可任意混用：统一解码为大整数做集合运算
（Python 大整数按机器字并行，RAW×RAW 百万位 AND/OR 仅 ~0.2 ms），
结果掩码到 n 位后按第 2 节规则重新选编码。对 8 组数据对 × 4 种运算
（AND/OR/XOR/DIFF）× 4 种编码组合（RAW×RAW、RAW×SPARSE、SPARSE×RAW、
SPARSE×SPARSE，含 NSPARSE 稠密位）共 128 条，与参考实现
（独立的 int 集合运算 + 掩码）逐条比对位级相等。**143/143 全部 PASS**，
明细含结果 popcount 与 sha1，见 `difftest_results.csv`。样例：

```
AND  extreme_sparse & extreme_dense  任意编码组合  popcount=10    sha1=0100452dfcce  PASS
OR   extreme_sparse & extreme_dense  任意编码组合  popcount=99990 sha1=d4541308a9aa  PASS
DIFF extreme_sparse - extreme_dense  任意编码组合  popcount=0     sha1=5ba93c9db0cf  PASS
OR   empty & full                    任意编码组合  popcount=100000 sha1=81d86f185401  PASS
```

百万位混合运算实测（ms，含结果重编码）：

| 数据对 | 编码组合 | AND | OR | XOR | DIFF |
|---|---|---:|---:|---:|---:|
| (1e-4, 0.5) | SPARSE×RAW | 1.36 | 0.21 | 0.21 | 1.23 |
| (1e-4, 0.9999) | SPARSE×NSPARSE | 2.36 | 2.48 | 4.80 | 0.20 |
| (0.5, 0.9999) | RAW×NSPARSE | 0.19 | 1.29 | 0.20 | 1.33 |
| (0.5, 0.5) | RAW×RAW | 0.17 | 0.16 | 0.10 | 0.13 |

原始数据：`bench_results.csv`。结果编码同样随分布自动切换，如
SPARSE×RAW 的 AND 结果仍稀疏 → SPARSE（132 B），OR → RAW（125004 B）。

## 6. 边界用例

- 全空、全满：负载 0 字节，共 4 B；空集参与 AND/DIFF、全集参与 OR 均验证。
- 单个位：第 0 位、最后一位（n-1），验证边界字节与掩码（最后一字节多余位清零）。
- 极稀疏（10 位/10 万）、极稠密（仅缺 10 位）：分别走 SPARSE / NSPARSE。
- 随机分布 6 档密度 + 交替位 1010… + 头部/尾部连续块。
- nbits 不一致的运算抛 `ValueError`；位置越界（-1、n）抛 `ValueError`。
- `serialize()/deserialize()` 往返一致（含编码与 negated 标志）。

## 7. 运行命令

```bash
python3 test_bitmap.py   # 自测 + 143 条对拍，生成 difftest_results.csv
python3 bench.py         # 百万位选择表/空间/耗时，生成两个 CSV
```

## 8. 交付清单

- `bitmap_codec.py` — 库：两种编码、自动选择、交并差混用、序列化
- `test_bitmap.py` — 自测与对拍（unittest，固定随机种子，可复现）
- `bench.py` — 百万位基准
- `difftest_results.csv` — 143 条对拍数据（解码一致性 15 条 + 混用运算 128 条）
- `encoding_table.csv` — 编码选择表与空间/编解码耗时
- `bench_results.csv` — 混合运算耗时与结果编码
- `REPORT.md` — 本报告

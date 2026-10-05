# 多级稳定排序：规则、对拍与基准

实现：`stable_sort.py`（Python 3，仅标准库）。排序键按优先级从高到低给出，
`("键名", True)` 表示该级降序，缺省升序。

## 平局规则（确定性保证）

1. 逐级比较：第 1 级不同则由第 1 级决定；相等则看下一级，依此类推。
2. 所有显式键都等价时，**保持输入中的原始先后**（稳定排序兜底）。
   实现上每个元素的排序键元组末尾附带原始下标，因此等价元素的相对顺序
   由原始下标唯一确定，不依赖输入分布、哈希种子或运行次数。
3. 同一批数据重复排序结果逐元素一致（对拍见下，基准中做了 100 万行验证）。

## 缺失 / 不可比键的回退（确定结果）

- **缺失**：键不存在、值为 `None`、值为 `NaN` 统一视为缺失，
  **无论升序还是降序都恒排最后**，缺失组内保持原始相对顺序。
- **不可比类型**：同一级出现不可比类型（如 `int` 与 `str`）时按类型秩回退：
  `数字(0) < bool(1) < str(2) < 其它(3, 按 repr)`，同秩内按正常值比较。
  回退后仍等价则走原始下标兜底。任何输入都不会抛 `TypeError`。

回退结果示例（`test_stable_sort.py` 中有对应断言）：

```
输入 id 序列:  k=[2, None, 1, <缺键>, NaN, 2]
升序结果 id :  [2, 0, 5, 1, 3, 4]   # 1 < 2=2，缺失(None/缺键/NaN)恒在最后且保原序
降序结果 id :  [0, 5, 2, 1, 3, 4]   # 缺失同样在最后，不随降序翻到最前

不可比混合:    k=["z", 9, {"a":1}, True, 1, "a"]
升序结果 id :  [4, 1, 3, 5, 0, 2]   # 数字(1,9) -> bool -> str(a,z) -> dict(repr)
```

## 与“按主键重排”的差异说明

设 `multi = stable_sort(data, [k1, k2, ...])`：

- 对 `multi` 再**只按主键 k1 稳定重排**，结果不变（恒等操作）——
  因为多级结果本身就是主键有序、且主键相等块内已按次级键有序。
- 对**原始数据**只按主键排序 `stable_sort(data, [k1])`，与 `multi` 的关系：
  主键值序列完全一致、行集合完全一致，**唯一差异在主键相等块内部的顺序**——
  前者块内是原始顺序，后者块内按 k2..kn 细化。这正是分页去重的关键：
  只按主键排时块内顺序若不稳定，翻页边界就会重复/漏项；多级稳定排序
  把块内顺序也钉死，任意一页的内容都可复现。
- 可执行证明：`test_primary_key_resort_explained`。

## 对拍数据

- 实现对拍：被测实现（一次性键元组排序）与独立参考实现
  （多趟稳定排序，低优先级键先排）在 5 组随机种子 × 5000 行混合数据上
  逐元素一致（`test_cross_check_with_reference`）。
- 冻结数据：`testdata/pair_input.json`（60 行，含 None/缺键/bool/str/list/dict）
  与 `testdata/pair_expected.json`（期望输出），
  `test_frozen_pair_data` 校验两种实现都等于冻结结果。
- 重复排序：`test_repeated_sort_identical` 对 10000 行连排 4 次逐元素一致，
  且对已排序结果再排为恒等（幂等）。

## 覆盖情形

| 情形 | 测试 |
|---|---|
| 全部平局 | `test_all_ties_keep_original_order` |
| 完全没有平局 | `test_no_ties_at_all` |
| 部分键为空（None/缺键/NaN） | `test_missing_keys_always_last_and_deterministic` |
| 键不可比（int/str/bool/dict 混合） | `test_incomparable_keys_fallback` |
| 多级 + 升降序混合 | `test_multi_level_and_directions` |

## 耗时与内存（100 万行 × 10 级键，升降序交替，含 5% 缺失）

机器：Python 3.12.3，Linux，16 GiB 内存。

| 指标 | 数值 |
|---|---|
| 排序耗时（无追踪） | 27.96 s |
| 排序耗时（tracemalloc 开启） | 62.77 s |
| 排序期间 Python 堆增量峰值（tracemalloc） | 1508 MiB |
| 进程 RSS 峰值（含数据生成） | 5290 MiB |
| 重复排序一致性 | 100 万行逐元素一致（基准内置断言） |

## 运行命令

```bash
python3 -m unittest test_stable_sort -v   # 自测 + 对拍（9 项）
python3 bench.py                          # 100 万行 x 10 级键基准
```

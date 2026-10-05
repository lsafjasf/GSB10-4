# 对数域贝叶斯更新：数值说明

离散假设空间 H = {h₀,…,h_{K-1}}，先验 p(hᵢ)，第 t 次观测 x_t 的似然 Lᵢ⁽ᵗ⁾=p(x_t|hᵢ)。

## 1. 朴素做法为什么会下溢

概率域递推 `w_i *= L_i; post = w / sum(w)`：连乘权重随观测数指数衰减。
double 最小正规数 ≈ 2.2e-308，只要 `log w_i < -745` 就变成 0.0。
全部权重归零后归一化是 0/0；即使部分归零，被归零的假设**永久丢失**，
之后无论收到多少有利于它的观测都无法恢复。实测似然比 1e-2 时 400 步即归零
（见 `REPORT.txt` 第 3 节）。

## 2. 稳定方案（本库 `bayes_stable.py`）

**(a) 状态全部在对数域。** 内部维护未归一化对数权重

```
s_i = log p(h_i) + Σ_t log L_i^(t)
```

每次更新只是一次加法 `s_i += log L_i`。对数似然的表示范围是
[-∞, 0]：似然小到 5e-324 也只对应 log 值 ≈ -744；用对数接口
`observe_log` 时 log L = -1e6 甚至 -1e300 都是有限数，**不存在连乘，
因此不存在下溢**。似然恰为 0 时 log L = -∞，表示该假设被证据精确排除。

**(b) 归一化用 log-sum-exp（减最大值技巧）。**

```
m      = max_i s_i
log Z  = m + log( Σ_i exp(s_i - m) )
log p_i = s_i - log Z
```

先减最大值保证 `exp` 的参数 ≤ 0，不会上溢；最大项始终贡献 1，即使其他项
小到 `exp` 下溢为 0，log Z 仍精确等于最大项（此时归一化只差可忽略的量）。
本库不做“每步重归一化”，归一化只在查询后验时做一次，减少累积舍入；
总对数证据 `log p(x₁..xₙ) = Σ log Z_t` 一并累加。

**(c) 权威输出是对数后验；转线性必须过下溢检查。**
`exp(log p)` 在 log p < -745.13 时下溢。本库拒绝静默返回 0.0：

- `result.prob`：有任一项下溢即抛 `PosteriorUnderflowError`；
- `result.has_underflow` / `result.underflow_records`：记录假设、log p、
  与最大值的差距，并追加到 `filter.underflow_log`；
- `result.prob_or_zero()`：下溢项填 0.0，仅用于报表，调用方必须检查标志。

**(d) 精度监护（区别于下溢）。** double 只有 ~15–16 位有效数字。若未归一化
对数状态跨度 > 1e12，则在 log-sum-exp 求和里小权重项被大项吞没（加法失效），
此时记录 `precision-loss-guard` 警告——这是精度问题，远早于任何下溢发生。

## 3. 退化输入的确定行为

| 输入 | 行为 |
|---|---|
| 先验含 0 | 合法，log 0 = -∞，该假设永久排除（观测无法复活） |
| 先验全 0 / 和≤0 / 空 | 构造抛 `DegeneratePriorError` |
| 先验负、NaN、+∞ | 构造抛 `DegeneratePriorError` |
| 单次似然为 0 | 合法，对应假设精确排除 |
| 某观测全部似然为 0（Z=0） | 抛 `ZeroEvidenceError`，状态回滚到该观测之前 |
| 似然负、NaN、+∞ | 抛 `ValueError`，状态不变 |

## 4. 对拍方法与误差数据

参考实现（仅测试代码使用）：
`fractions.Fraction` 精确有理数 —— 后验 = 先验与全部似然的精确乘除；
极端对数输入用 `decimal` 50 位精度独立计算。两者都不经过 double 乘除。

指标：对数后验最大绝对/相对误差、概率最大绝对误差、总变差 TV。
最新结果见 `REPORT.txt`（机器：Python 3.12 / linux）：

- 单次更新 K=3：log 误差 4.4e-16，TV 6.2e-17（机器精度）
- 500 次随机观测 K=6：log 相对误差 2.5e-14，TV 1.3e-14
- 20 次观测、似然 1e-300 vs 1（线性接口）：log 相对误差 4.0e-16；
  朴素实现第 1 步即 [0,1] 全灭
- 30 次观测、log 似然 -1e6 vs 0：log 相对误差 1.2e-16（绝对 3.7e-9 即
  log p≈-3e7 量级上的 1 个 ulp），线性概率 exp(-3e7) 超出 double 范围

注：对 log p 量级达 1e3–1e7 的量，绝对误差由 ulp 决定，必须用相对误差判读。

## 5. 100 万次观测基准

K=10，观测流式生成（不存历史），1,000,000 次 `observe`：

- 耗时约 10.8 s 总量，约 10.9 µs/次（含似然生成），单次更新 O(K)
- Python 堆峰值 3.8 KiB（tracemalloc，与观测数无关），
  进程 RSS 15.5 MiB（含解释器）——状态内存 O(K)
- 后验极度集中（次优假设 log p≈-7.2e5）：`result.prob` 按要求抛
  `PosteriorUnderflowError`，而 log p 全部有效
- 独立重放 + `math.fsum` 复核：1e6 次加法后 log 后验最大漂移 4.7e-10

## 6. 运行命令

```bash
python3 selftest_bayes.py                  # 运行全部自测与基准，断言退出码非 0 即失败
python3 selftest_bayes.py > REPORT.txt     # 重新生成数据报告
```

库用法：

```python
from bayes_stable import BayesFilter

f = BayesFilter(prior=[0.5, 0.5])
f.observe([1e-300, 1.0])          # 极小线性似然
f.observe_log([-1e6, 0.0])        # 任意小的对数似然（推荐）
res = f.posterior()
res.log_prob                      # 权威输出，永远有效（可能含精确的 -inf）
res.has_underflow                 # 转线性是否会下溢
# res.prob                        # 会下溢时抛 PosteriorUnderflowError，绝不静默返回零
```

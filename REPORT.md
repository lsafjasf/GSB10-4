# 句柄/连接泄漏检测：交付报告

语言：Python 3（仅标准库）。环境：Python 3.12.3，Linux x86_64，16 核。

## 文件

| 文件 | 说明 |
|---|---|
| `src/leakdetect.py` | 检测库：`LeakTracker`（获取/释放跟踪、按获取点归因、泄漏判定、核算、报告） |
| `src/test_leakdetect.py` | 自测：7 个场景，覆盖全部要求情形 |
| `src/bench_leakdetect.py` | 基准：100 万对获取/释放的开销与内存上界 |

## 运行命令

```bash
python3 src/test_leakdetect.py     # 自测（约 1 秒）
python3 src/bench_leakdetect.py    # 基准，100 万对（约 1 分钟，full 模式栈捕获较慢）
python3 src/bench_leakdetect.py 100000   # 可用参数缩小规模
```

## 设计：按获取点归因，而非只报总数

每次 `acquire(handle)` 记录一条获取记录：`(handle, site, acquired_at, exception, stack)`，
存入 `_live` 表（key 为 `id(handle)`）。`site` 缺省由 `sys._getframe` 自动取**库外调用方**
的 `文件:行号 函数名`；`stack` 为完整调用栈（`traceback.extract_stack`，库内部帧已过滤），
按站点去重存储。`release(handle)` 从 `_live` 核销。泄漏判定 = 仍在 `_live` 中且越过
存活阈值的记录，因此每条泄漏天然携带获取点标识与调用栈。

## 判定依据与误报排除

一条记录被判为泄漏需同时满足：

1. **引用计数依据**：该句柄在跟踪表中仍有 1 条未核销的获取记录
   （`acquire - release > 0`，即 `_live` 命中）——这是"未释放"的直接证据；
2. **存活时长依据**：`now - acquired_at >= age_threshold`——排除"正在正常使用中"
   的句柄（阈值按业务正常持有时长设定，默认 5s）。

误报排除机制（自测场景 5 验证）：

- **阈值排除**：未超 `age_threshold` 的在用句柄不判泄漏；
- **宽限确认** `confirm(grace_period)`：首次判定的疑似泄漏等待一个宽限期后复查，
  宽限期内已被正常释放的记为误报并排除（返回 `cleared` 计数），仍存活的才确认。
  对应"判定时刻恰好处于释放前夕"的竞态。

## 正常/异常两类路径

- `release(h)` 计入正常路径释放；`release(h, exception=exc)` 计入异常路径释放，
  两者分别统计（`normal_releases` / `exception_path_releases`）。
- 异常路径泄漏：`note_exception(h, exc)` 把异常信息挂到获取记录上，泄漏报告中
  该条泄漏携带 `关联异常`，可直接定位是哪次失败导致没走到释放。
- `tracker.wrap(h)` 提供上下文管理器，`with` 退出（含异常退出）自动按对应路径释放。

## 自测结果（7 场景全部通过）

```
[PASS] 场景1 全部正常释放: 0 泄漏, 核算 100 - 100 = 0, 正常路径释放=100
[PASS] 场景2 部分泄漏: 3 条泄漏全部归因到 .../test_leakdetect.py:39 site_leaky_acquire
       判定依据: age=0.060s>=阈值, 引用计数>0
[PASS] 场景3 异常路径: 异常路径释放=2, 异常路径泄漏=2 (泄漏记录携带异常信息)
[PASS] 场景4 重复释放: raise 策略抛 DoubleReleaseError/UnknownHandleError 并计数;
       record 策略只计数不抛错
[PASS] 场景5 误报排除: 阈值内不判泄漏; 宽限确认排除误报1条, 确认真实泄漏1条
[PASS] 场景6 上下文管理器: 正常释放=1, 异常路径释放=1, 核算 2 - 2 = 0
[PASS] 场景7 核算: acquired=708 - released=703 = leaked=5 (balanced=True)
```

## 归因报告样例（场景 7 实际输出，节选）

```
=== 泄漏归因报告 [t7] ===
核算: acquired=708 released=703 live=5  (708 - 703 = 5, balanced=True)
释放路径: 正常=700 异常=3 重复释放=0 未知句柄=0
判定依据: 存活时长阈值=0.05s 且 跟踪表引用计数>0

泄漏 5 条，按获取点归因：
  [1] 获取点: .../test_leakdetect.py:39 site_leaky_acquire  (泄漏 5 条)
      存活时长: 0.060s ~ 0.060s (阈值 0.05s, age_ok=True, 引用计数>0)
      获取调用栈(代表):
        File ".../test_leakdetect.py", line 211, in test_accounting
          leaked = site_leaky_acquire(t, 5)
        File ".../test_leakdetect.py", line 39, in site_leaky_acquire
          leaked.append(tracker.acquire(FakeHandle("conn")))

按获取点聚合：
  ...:31 site_normal_acquire:    acquired=700 released=700 leaked=0
  ...:39 site_leaky_acquire:     acquired=5   released=0   leaked=5 oldest_age=0.060s
  ...:47 site_exception_acquire: acquired=3   released=3   leaked=0
```

## 核算数据

不变式：`acquired - released == live == leaked`，每次 `stats()` 都校验并输出
`balanced` 标志；自测 7 个场景与基准 100 万对全部平账：

- 自测场景 7：`708 - 703 = 5`，balanced=True
- 基准 full 模式：`1000000 - 1000000 = 0`，balanced=True，正常释放=1000000

按获取点也有独立核算：`per_site[site] = {acquired, released, leaked, double_releases}`。

## 开销与内存上界（100 万对获取/释放实测）

计时区间关闭 GC 与 tracemalloc；内存单独测量，避免相互干扰。

| 模式 | 吞吐 | 净开销（相对不跟踪基线） |
|---|---|---|
| 基线（不跟踪） | 34.77 M对/s (0.029 µs/对） | — |
| lean（仅获取点标识） | 1.36 M对/s (0.734 µs/对） | **0.705 µs/对** |
| full（含完整调用栈） | 0.09 M对/s (10.6 µs/对） | **10.6 µs/对**（栈按站点去重只存一次） |

内存（100 万对全部释放后 + 10 万条泄漏驻留实测）：

- **稳态（全部释放）**：只剩站点统计与去重调用栈，与总获取次数无关，O（站点数）；
  实测 RSS 增量 lean=1.37 MB / full=0.78 MB（10 个站点）。
- **未释放记录驻留**（含 32 B 句柄对象本身）：
  lean ≈ **280 B/条**，full ≈ **715 B/条**（tracemalloc 精确值）。
- **内存上界**：`M ≤ 每条字节数 × 未释放句柄数 L + O(站点数 S × 栈深度)`，
  与累计获取次数无关；10 万条泄漏实测 lean 28.0 MB / full 71.5 MB。
  生产建议用 lean 模式常驻，检出泄漏后按需切 full 模式取调用栈。

## 使用示例

```python
from leakdetect import LeakTracker

tracker = LeakTracker("db-conn", age_threshold=30.0)   # 正常持有不超过 30s

def handler():
    conn = tracker.acquire(pool.get())                 # 获取点自动归因到本行
    try:
        conn.execute(...)
    except Exception as exc:
        tracker.note_exception(conn, exc)              # 异常路径标注
        raise
    finally:
        tracker.release(conn)                          # 或 except 中传 exception=exc

# 周期任务：
leaks = tracker.check()                # 即时判定（阈值+引用计数）
confirmed, cleared = tracker.confirm() # 宽限确认，排除误报
print(tracker.report())                # 归因报告
assert tracker.stats()["balanced"]     # 核算
```

# 句柄/连接泄漏检测库（leakdet）— 归因报告与判定依据

纯 Python 3 标准库实现（`threading` / `weakref` / `traceback` / `collections`），
无第三方依赖。文件：

- `leakdet.py` — 检测库（`LeakTracker` / `Handle` / `LeakReport`）
- `test_leakdet.py` — 14 个自测（unittest）
- `demo.py` — 模拟业务场景，输出归因报告
- `bench.py` — 100 万次获取/释放的开销与内存压测

## 运行命令

```bash
python3 -m unittest -v test_leakdet   # 自测（14 个用例）
python3 demo.py                       # 归因报告演示
python3 bench.py                      # 100 万次开销/内存压测
```

## 泄漏如何归因到获取点

每次 `acquire()` 记录：唯一句柄 id、获取时间戳、显式 label、以及获取处的
调用栈（`traceback.extract_stack`，深度可配，可关闭）。`check_leaks()` 返回的
报告按获取点分组，每个泄漏句柄附带完整获取调用栈（文件、行号、函数），
而不是只有一个总数。`demo.py` 的实际输出节选：

```
leaks: 4 handle(s), grouped by acquisition site:
  [3x [abandoned]] db:worker_leaky (ids=[61, 62, 63], max_age=0.000s)
    --- acquire stack for handle #61 ---
      File ".../demo.py", line 60, in main
        worker_leaky(t)
  [1x [abandoned]] db:worker_exception_leaky (ids=[64], ...)
double-release events: 1
    hid=65 at .../demo.py:62 (main) note=cleanup again
```

## 未释放的判定依据

三条独立判据，可组合：

1. **引用计数**：句柄获取时 refcount=1，`retain()` 加一、`release()` 减一；
   检查时刻 refcount > 0 且仍在存活集合中 → 未释放。
2. **存活时长阈值**：`check_leaks(max_age=秒)` 只把存活超过阈值的句柄判为泄漏，
   避免把"正在正常使用中"的句柄误报。
3. **对象终结（确定性泄漏）**：tracker 对 `Handle` 门面只持弱引用；若业务代码
   把 Handle 对象丢弃（GC 回收）却从没 release，weakref 终结器把该条目标记为
   `abandoned` —— 此时已无任何途径再释放它，是**确定性泄漏**，不是嫌疑。

**误报排除**：`check_leaks(exclude=谓词)` 可白名单合法的长寿命句柄（如连接池
常驻连接，`label` 以 `pool:` 开头者）；配合 `max_age` 阈值，短于阈值的在用句柄
不报。被排除的句柄仍计入核算，不会丢失。

## 正常关闭与异常路径的释放记录

两类路径都会写入有界释放日志（`release_log`，含获取点、释放备注、持有时长）：

- 正常关闭：`h.release(note="normal close")` 或 `with` 块正常退出
  （备注 `ctx-exit`）。
- 异常路径：`with` 块抛异常时 `__exit__` 仍释放（备注 `ctx-exit:RuntimeError`
  等异常类型名）；`finally` 中释放也照常被记录。`demo.py` 输出中可见
  `note=ctx-exit:RuntimeError` 的释放记录。
- 异常路径**漏**释放（`worker_exception_leaky`）则被检测为泄漏并归因到获取点。

## 核算数据（可复算）

不变式：**acquired − released = live = 泄漏数**（重复释放不计入 released，
单独计数，不会把计数冲成负数）。`demo.py` 实测：

```
acquired=67 released=62 live=5 balanced=True
```

核算：67 − 62 = 5 = 4 个泄漏（3 个 `worker_leaky` + 1 个异常路径泄漏）
+ 1 个合法池连接（被 exclude 排除出泄漏名单，但仍在 live 中）。测试
`TestAccounting` 用 50 取 38 放验证 `acquired − released == leaks == 12`。

## 开销数据（100 万次获取/释放，bench.py 实测）

环境：Python 3.12.3，Linux x86-64 容器。

| 模式 | 总耗时 | 单次获取+释放 | tracker 内存峰值 | 进程 RSS 增量 |
|---|---|---|---|---|
| 生产模式（不抓栈，仅 label） | 6.82 s | 6.82 µs | 0.25 MB | 0.00 MB |
| 调试模式（抓 8 层调用栈） | 47.04 s | 47.04 µs | 0.28 MB | 0.00 MB |

- 1 万次存活句柄的一次泄漏检查：2.87 ms。
- **内存上界**：O(存活句柄数) + O(release_log_size=1000) + O(1) 计数器。
  已释放句柄的状态（含调用栈）立即丢弃，释放日志是有界 deque，因此 100 万次
  稳态churn下 tracker 内存为常数（实测 RSS 零增长）。存活句柄的单条状态
  约 250–300 字节（不抓栈），即 1 万个泄漏句柄约 3 MB 上限。
- 生产建议：常态用 `capture_stacks=False`（6.8 µs/对），发现泄漏后对可疑
  获取点临时开栈抓取定位。

## 场景覆盖矩阵（test_leakdet.py，14 用例全绿）

| 场景 | 用例 |
|---|---|
| 全部正常释放 | `TestFullRelease`（含释放日志记录获取点/备注/持有时长） |
| 部分泄漏 | `TestPartialLeak`（按获取点分组归因、无 label 时按文件:行号归因） |
| 异常路径释放被记录 | `TestExceptionPath`（finally 释放、with 块异常退出释放） |
| 异常路径泄漏被检出 | `TestExceptionPath.test_exception_skipping_release_is_detected` |
| 重复释放 | `TestDoubleRelease`（记录事件、不破坏核算） |
| 引用计数 | `TestRefcount`（retain 需配对 release；对已死句柄 retain 报错） |
| 丢弃未释放（确定性泄漏） | `TestAbandonedHandle` |
| 判定依据/误报排除 | `TestJudgementBasis`（时长阈值、exclude 白名单） |
| 核算不变式 | `TestAccounting` |

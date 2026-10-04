# corolocal — 协程本地存储（派生时复制，Copy-On-Spawn）

Python 3 标准库实现（`contextvars` + `asyncio` + `copy`），无第三方依赖。

## 文件

| 文件 | 说明 |
|---|---|
| `corolocal.py` | 库源码 |
| `test_corolocal.py` | 自测（12 个用例，unittest） |
| `layer_demo.py` | 多层派生逐层对拍数据打印 |
| `bench_corolocal.py` | 100 万次派生+读取基准 |

## 复制语义（Copy Semantics）

1. **每个任务一个独立 Store。** 每个协程任务在自己的 `contextvars.Context`
   中绑定一个 `Store` 对象；`asyncio.create_task` 会复制 Context，容器天然隔离。
2. **派生点深拷贝。** `spawn()` 在**调用时刻**（同步、原子）对父 Store 做
   `copy.deepcopy`，子任务拿到完全独立的副本——包括所有嵌套可变对象
   （dict / list / 自定义对象）。子任务内 `set` / `delete` 以及对可变值的
   **原地修改**，都不会影响父任务或任何兄弟任务。
3. **快照时机确定。** 快照在 `spawn()` 被调用的瞬间拍定；父任务之后再修改，
   已派生的子任务读到的仍是旧快照（见 `test_child_starts_from_call_time_snapshot`）。
4. **无共享引用。** 父子、兄弟之间不存在任何共享的可变状态，因此不存在
   “子任务修改串回父任务”的通道。

## 显式传递 vs 隐式传播

| | 隐式传播 | 显式传递 |
|---|---|---|
| API | `cl.spawn(coro)` | `cl.spawn(coro, store=s)` / `cl.use_store(s, fn)` / `cl.fork()` |
| 数据来源 | 调用方**当前** Store 的深拷贝 | 给定 Store `s` 的深拷贝，与当前上下文无关 |
| 适用场景 | 常规结构化并发：子任务继承父任务的请求级状态（trace id、租户、配置） | 跨层级/跨模块传递指定快照；给子任务一个隔离的“干净”视图（如传空 Store 隐藏父状态）；把快照存起来稍后复用 |

两种方式子任务拿到的都是**深拷贝副本**，源 Store 永远不被修改。

## 逐层对拍数据（`python3 layer_demo.py` 实测输出）

场景：root 设 `a=1, obj.v=0, level=root`；派生 child 与 sibling；child 内再派生 grand。

```
层       观测点                             实际 (a,obj.v,level)          手工推算                 对拍
root    初始                              (1, 0, 'root')              (1, 0, 'root')          ✓
child   进入时（=根层快照）                 (1, 0, 'root')              (1, 0, 'root')          ✓
child   修改后                            (2, 20, 'child')            (2, 20, 'child')        ✓
sibling 进入时（=根层快照）                 (1, 0, 'root')              (1, 0, 'root')          ✓
sibling 修改后                            (9, 0, 'sibling')           (9, 0, 'sibling')       ✓
grand   进入时（=子层快照）                 (2, 20, 'child')            (2, 20, 'child')        ✓
grand   修改后                            (3, 30, 'grand')            (3, 30, 'grand')        ✓
child   孙任务结束后（不得回灌）             (2, 20, 'child')            (2, 20, 'child')        ✓
root    全部子任务结束后（不得回灌）          (1, 0, 'root')              (1, 0, 'root')          ✓
```

手工推算模型：每层 = 父层 dict 的 `deepcopy` 再做本层修改（`layer_demo.py`
中的 `reference_chain()`），与库的实际观测逐格一致。

## 耗时与内存（100 万次派生 + 读取）

实测环境：Python 3.12.3，Linux x86-64。Store 内含一个标量键 + 一个嵌套
可变值（`{"v": [1,2,3]}`），让深拷贝有真实工作量。

| 场景 | N | 总耗时 | 单次 | 峰值内存 (tracemalloc) | 进程峰值 RSS |
|---|---|---|---|---|---|
| 顺序派生真实 Task（`spawn`+`await`） | 1,000,000 | 25.27 s | 25.27 µs | 0.00 MB（有界） | 34.45 MB |
| 纯派生快照（`fork()`+`get`，不开 Task） | 1,000,000 | 9.48 s | 9.48 µs | 0.00 MB（有界） | 19.97 MB |
| 并发派生（10 万任务同时存活） | 100,000 | 4.34 s | 43.40 µs | 123.96 MB | 285.93 MB |

解读：顺序派生下内存恒定（每轮任务结束后即回收）；单次开销中约 16 µs
是 `asyncio.Task` 创建成本，深拷贝快照本身约 9.5 µs。并发场景内存随同时
存活任务数线性增长（≈1.2 KB/任务的解释器对象 + 快照副本）。

## 边界用例与确定行为

| 情形 | 行为 |
|---|---|
| 无上下文 `get(key)` | 返回 `default`（默认 `None`），不抛异常 |
| 无上下文 `require(key)` / `delete(key)` | 抛 `KeyError`（确定行为，不返回脏值） |
| 无上下文 `set(key, v)` | 惰性创建根 Store 并绑定到当前 Context；仅当前 Context 可见 |
| 无上下文 `fork()` | 返回空 Store |
| 兄弟任务并发交错修改 | 各自持有独立副本，互不可见；父任务保持派生时的值（见 `test_siblings_isolated`，含 `await asyncio.sleep(0)` 强制交错） |
| 子任务原地修改可变值 | 深拷贝隔离，父任务持有的引用与 Store 内容均不变 |
| `spawn` 后父任务再修改 | 子任务读到调用时刻的旧快照 |
| 并发修改安全性 | asyncio 单线程协作式调度，单任务内 get/set 原子，无抢占、无脏读；本库不保证跨线程/多事件循环使用 |

## 运行命令

```bash
cd corolocal
python3 test_corolocal.py        # 自测（12 用例）
python3 layer_demo.py            # 逐层对拍数据
python3 bench_corolocal.py       # 100 万次基准（约 30 秒）
python3 bench_corolocal.py 100000  # 自定义次数
```

## 快速上手

```python
import asyncio
import corolocal as cl

async def child():
    cl.set("a", 2)              # 只影响自己的副本
    return cl.get("a")

async def main():
    cl.set("a", 1)
    r = await cl.spawn(child()) # 隐式传播：深拷贝当前 Store
    assert r == 2 and cl.get("a") == 1

    snap = cl.fork()            # 显式：拿一份独立快照
    snap.set("tenant", "t1")
    await cl.spawn(child(), store=snap)  # 显式传递

asyncio.run(main())
```

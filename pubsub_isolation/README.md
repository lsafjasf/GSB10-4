# 进程内发布订阅（订阅者异常隔离）

纯 Python 3 标准库实现，无第三方依赖。

## 文件

- `pubsub.py` — 库：`EventBus` / `PublishResult` / `SubscriberError`
- `test_pubsub.py` — 自测（9 个用例）
- `benchmark.py` — 基准：1000 订阅者 × 10 万次发布
- `demo.py` — 异常场景投递结果演示

## 运行命令

```bash
cd pubsub_isolation
python3 -m unittest test_pubsub -v   # 自测
python3 benchmark.py                 # 基准
python3 demo.py                      # 异常场景演示
```

## 语义约定

- **投递顺序确定**：严格按订阅顺序（`OrderedDict` 保证），先订阅先投递。
- **不并发投递**：同步串行单线程分发循环。顺序保证与"取消立即生效"
  都依赖串行语义；若未来要并发，需以订阅者为单位隔离并放弃严格顺序。
- **异常隔离**：每个订阅者的调用单独 `try/except BaseException`，
  异常不会逃出分发循环，后续订阅者照常收到事件。
- **取消立即生效**：`unsubscribe` 直接从活订阅表删除；分发循环在调用
  每个订阅者前重新查表，本轮内被取消的订阅者跳过并记入 `skipped`。
- **本轮新订阅者不接收本轮事件**：本轮候选集合在发布开始时确定。

## 异常汇总结构

`publish()` 返回 `PublishResult`，不抛异常（可选 `raise_if_failed()`
转为 `ExceptionGroup`）：

```
PublishResult
├── topic, event
├── delivered: list[int]        # 成功投递的订阅者 id，按投递顺序
├── skipped:  list[int]         # 本轮内被取消而跳过的订阅者 id
└── errors:   list[SubscriberError]   # 全部异常，逐条，不只报一条
      ├── subscription_id       # 订阅者标识
      ├── handler_name          # 订阅时给的名字或函数限定名
      ├── error                 # 原始异常对象
      ├── error_type            # 异常类名
      └── traceback_str         # 完整堆栈
```

## 异常场景投递结果（demo.py 实测）

4 个订阅者中 bob/dave 抛错，alice/carol 照常收到：

```
正常收到事件的订阅者: [('alice', {'order_id': 42}), ('carol', {'order_id': 42})]
delivered ids        : [1, 3]
errors (全部汇总)    :
  sub#2 bob: ValueError: db down
  sub#4 dave: RuntimeError: timeout
```

## 基准数据（benchmark.py 实测）

环境：Python 3.12.3，Linux x86_64 容器。

| 指标 | 数值 |
|---|---|
| 订阅者 / 发布次数 | 1000 / 100,000 |
| 总投递次数 | 100,000,000 |
| 总耗时 | 5.62 s |
| 单次发布 | 56.2 µs |
| 单次投递 | 56.2 ns（≈17.8 M 投递/秒） |
| Python 堆峰值（tracemalloc） | 0.15 MiB |
| 进程 RSS 峰值（ru_maxrss） | 32.6 MiB（与基线持平，无随轮次增长） |

结果对象每轮新建、无跨轮累积状态，因此内存峰值与发布次数无关。

## 覆盖的边界用例（test_pubsub.py）

1. 单个订阅者抛错，其余照常收到且顺序不变
2. 多个订阅者抛错，全部按订阅顺序汇总（不只报一条）
3. 投递中取消订阅，本轮剩余投递不再调用该订阅者
4. 两轮之间取消订阅，下一轮不再投递
5. 没有任何订阅者时发布，返回空结果且不报错
6. 20 个订阅者验证投递顺序 == 订阅顺序
7. 本轮新订阅者不参与本轮投递
8. `raise_if_failed()` 聚合为 `ExceptionGroup`
9. `KeyboardInterrupt` 等 `BaseException` 同样被隔离

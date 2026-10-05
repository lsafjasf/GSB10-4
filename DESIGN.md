# 长事务资源控制设计说明

纯 Python 3 标准库实现，时间全部通过 `ManualClock` 注入，测试可精确控制。

## 1. 清理水位（water level）与最老活跃事务的关系

水位定义（`longtxn/txn.py` `TxnManager.water_level()`）：

```
water_level = min(start_ts of ACTIVE transactions)   # 有活跃事务
water_level = clock.now()                            # 无活跃事务
```

即：**水位 = 最老活跃事务的开始时间戳**。最老活跃事务是水位的唯一决定者，
任何一个长事务不结束，水位就停在它的 `start_ts` 上不动。

水位推进规则：

1. 水位单调不减，只能随"最老活跃事务"的结束（提交/中止/被强制中断）而前进。
2. 事务提交或中止后，若它是最老活跃事务，水位立即前进到下一个最老活跃事务
   的 `start_ts`；没有活跃事务时直接前进到当前时间。
3. 长事务被强制中断（`FORCED_ABORT`）等价于中止，同样释放水位。

清理不越线规则（`longtxn/store.py` `MVCCStore.gc()`）：

- 对每个 key，删除所有 `commit_ts <= water_level` 的版本，**但保留其中最新
  的一条**——即"水位处可见"的那个版本；
- `commit_ts > water_level` 的版本一律不动。
- 因此任意 `ts >= water_level` 的快照读在 GC 前后结果完全一致，GC 幂等。

## 2. 长事务识别

`TxnManager.scan()` 按注入时间计算事务年龄 `age = now - start_ts`：

| 条件 | 结果 |
|---|---|
| `age >= 30s`（`LONG_THRESHOLD_S`） | 标记为长事务，`identified_long_at` 落一次标记，扫描结果中返回 |
| `age >= 120s`（`KILL_THRESHOLD_S`） | 强制中断（见下节） |

识别是一次性的（避免重复告警），识别结果包含：事务 id、开始时间、被识别
时刻、当前活跃/被识别/被处置清单（`ScanResult.summary()`）。

## 3. 超长事务处置

策略：**限制时长 + 强制中断**（hard time limit）。活跃时间超过 120s 的事务
由 `scan()` 直接置为 `FORCED_ABORT` 并记录 `killed_at`；也支持运维手动
`force_abort()`。处置结果：

1. 事务状态 `ACTIVE -> FORCED_ABORT`，不再出现在活跃集合中；
2. 水位立即解除阻塞，推进到下一个最老活跃事务（或当前时间）；
3. 下一次 GC 回收它压住的全部历史版本（基准中单次回收 115,000 个版本）；
4. 处置决定先落盘（`state.json` 的 `gc_pending`），再开始清理，保证崩溃后
   不会"复活"一个已被杀掉的事务。

## 4. 崩溃恢复与重建

持久化协议（`longtxn/gc.py` `GCCoordinator`，目录下两个文件）：

```
state.json    事务表快照 + gc_phase(gc_pending/gc_done) + 水位
journal.json  {"phase": "started"|"done", "water_level": wl}
```

GC 顺序：`state(gc_pending) -> journal(started) -> store.gc -> journal(done)
-> state(gc_done)`。全部原子写（tmp + rename + fsync）。

恢复规则：

- journal 缺失或 `done`：无需处理；
- journal 为 `started`：清理在中途崩溃，按 `min(journal水位, 当前水位)`
  **重放** `store.gc()`（幂等），再补完协议；
- 活跃事务集从 `state.json` 重建，水位由重建后的最老活跃事务重新计算，
  与崩溃前完全一致（对拍数据见 `out/recovery_compare.txt`，全部 MATCH）。

## 5. 目录结构

```
longtxn/    库：clock / store(MVCC) / txn(事务+水位+识别+处置) / gc(清理+恢复)
tests/      7 个自测：无事务、短事务、单超长事务、水位不越线、
            崩溃重建对拍、清理中崩溃重放、清理中事务崩溃对拍
bench/      benchmark.py（100 万操作曲线）、recovery_compare.py（对拍数据）
out/        benchmark.csv、recovery_compare.txt
```

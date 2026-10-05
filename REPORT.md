# 长事务资源控制 — 自测结果与性能报告

运行环境：Python 3.12.3（仅标准库），时间全部由 `ManualClock` 注入。

## 1. 自测覆盖与结果

`python3 -m unittest discover -s tests -v` → **7 passed**：

| 测试 | 情形 | 验证点 |
|---|---|---|
| `test_gc_with_no_txns` | 无事务 | 水位=当前时间，10 版本回收 9、保留最新；GC 幂等 |
| `test_short_txn_does_not_block` | 短事务 | 提交后水位立即释放，旧版本可回收 |
| `test_identify_then_force_abort` | 单个超长事务 | 30s 识别、120s 强制中断、水位 0→120s、30→1 版本 |
| `test_water_level_never_crossed` | 水位不越线 | 水位 0 时一个版本都不能删；水位 3.0 时保留该时刻可见版本 |
| `test_recovery_rebuild_matches_precrash` | 崩溃重建对拍 | 重建后水位/活跃集/可见数据/版本数与崩溃前全部相等 |
| `test_crash_during_cleanup_replays` | 清理中崩溃 | journal=`started` 未完成，恢复后重放，结果与正常 GC 一致且幂等 |
| `test_txn_crashes_while_cleanup_runs` | 清理中事务崩溃 | 事务在 GC 进行中止，与正常中止路径的水位/回收数/可见数据对拍一致 |

## 2. 长事务识别与处置结果

场景：1 个 t=0 开始的事务，注入时间推进到 30s / 120s（见
`test_identify_then_force_abort`）：

```
t=0~29s   scan() -> identified=[]，状态 ACTIVE，水位=0，GC removed=0
t=30s     scan() -> identified_long=[txn#1]，identified_long_at=30，仍 ACTIVE
                    （识别一次性，再次扫描不再重复标记）
t=30~119s 水位仍=0，GC 不删任何版本（30 个版本原数保留）
t=120s    scan() -> force_aborted=[txn#1]，state=FORCED_ABORT，killed_at=120
                    水位 0 -> 120s，下一次 GC removed=29，版本 30 -> 1
```

阈值：长事务 `age >= 30s`，强制中断 `age >= 120s`（`longtxn/txn.py` 常量，
构造时可覆盖）。

## 3. 崩溃后重建对拍数据

`python3 bench/recovery_compare.py` 输出（完整文件：`out/recovery_compare.txt`）：

```
water_level:              A 0.0  == B 0.0   MATCH
active_txn_ids:           A [1]  == B [1]   MATCH
visible_at_water_level:   A {'alpha': None, 'beta': None} == B 同  MATCH
visible_at_now:           A {'alpha': 'a5', 'beta': 'b4'} == B 同  MATCH
version_count:            A 11   == B 11    MATCH
overall: MATCH
```

崩溃点选在"水位被长事务压在 0"时，对拍证明：恢复出的最老活跃事务、水位、
水位处及当前时刻的快照可见性、版本链数量，全部与崩溃前一致。另有清理中途
宕机（journal=started）重放对拍，见自测第 6 项。

## 4. 100 万操作的内存与清理耗时曲线

`python3 bench/benchmark.py`（墙钟约 7s）。1,000,000 次更新，10,000 个 key，
每次推进 1ms（注入时间共 1000s）；4 个长事务：3 个从 t=0 开始（120s 被杀），
1 个从 t=300s 开始（420s 被杀）。每 25,000 次操作扫描+清理一次。

```
versions   : ▂▄▆█▁▁▁▁▁▁▁▁▂▄▆█▁▁▁▁▁...   10000 ~ 109999
gc_ms      : 两个尖峰 252.6 / 280.8ms，稳态 35~45ms
py_heap_kb : 与 versions 同形，2925 ~ 18653 KB
maxrss_kb  : 进程峰值单调（Python 不归还 OS），最高 117 MB
```

关键采样（完整 41 行见 `out/benchmark.csv`）：

| ops | 注入时间 | 水位 | 存活版本 | 本次回收 | GC 耗时 | Python 堆 |
|---:|---:|---:|---:|---:|---:|---:|
| 25,000 | 25s | 0s | 25,000 | 0 | 4.3ms | 5.0 MB |
| 100,000 | 100s | 0s | 100,000 | 0 | 13.2ms | 16.9 MB |
| 125,000 | 125s | 125s | **10,000** | **115,000** | 252.6ms | 2.9 MB |
| 300,000 | 300s | 300s | 10,000 | 25,000 | 38.2ms | 3.0 MB |
| 400,000 | 400s | 300s | 109,999 | 0 | 12.4ms | 18.7 MB |
| 425,000 | 425s | 425s | **10,000** | **124,999** | 280.8ms | 3.0 MB |
| 700,000 | 700s | 700s | 10,000 | 25,000 | 37.3ms | 3.1 MB |
| 1,000,000 | 1000s | 1000s | 10,000 | 0 | 3.5ms | 3.1 MB |

曲线解读：

1. **0~120s**：3 个长事务把水位钉在 0，旧版本线性堆积到 10 万，GC 每次
   回收 0（不越线）；120s 处置后单次回收 115,000 个版本，堆从 16.9 MB 跌
   到 2.9 MB。
2. **300s 新长事务**开始后水位停在 300s，版本重新堆积到 11 万；420s 它被杀
   后再次释放。说明控制对"后来的长事务"同样有效。
3. **清理耗时**与待回收积压量成正比：积压回收尖峰约 250~280ms，无积压的
   稳态 GC 约 35~45ms（线性扫 1 万个 key 链 + bisect）。
4. `maxrss` 不回落是 CPython 内存池不归还 OS 所致；`tracemalloc` 的
   `py_heap_kb` 才反映真实存活对象，其曲线与版本数完全同步。

## 5. 运行命令

```bash
# 自测（7 个用例）
python3 -m unittest discover -s tests -v

# 崩溃恢复对拍数据 -> out/recovery_compare.txt
python3 bench/recovery_compare.py

# 100 万操作基准曲线 -> out/benchmark.csv
python3 bench/benchmark.py
```

## 6. 交付清单

- 源码：`longtxn/{clock,store,txn,gc}.py`
- 自测：`tests/test_longtxn.py`
- 水位规则说明：`DESIGN.md`
- 处置结果：本文 §2
- 重建对拍数据：本文 §3、`out/recovery_compare.txt`
- 耗时与内存数据：本文 §4、`out/benchmark.csv`

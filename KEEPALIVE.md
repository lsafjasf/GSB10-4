# 保活与探测判定（keepalive）

纯标准库 Python 3 实现。判定**只依赖注入的时间与事件**，不读真实时钟、
无随机性，同一事件序列必然得到同一判定序列。

## 文件

| 文件 | 内容 |
|---|---|
| `keepalive.py` | 库：事件/状态定义、`KeepaliveEngine`、`simulate` 回放器 |
| `test_keepalive.py` | 19 个自测（含独立参考模型对拍） |
| `bench.py` | 10 万连接耗时与内存基准 |
| `gen_sequences.py` | 生成 `sequences.json`（判定序列数据） |
| `sequences.json` | 4 个场景的逐时刻判定序列，引擎与参考模型逐点一致 |

## 运行命令

```bash
python3 -m unittest -v test_keepalive.py   # 自测
python3 gen_sequences.py                   # 重新生成判定序列数据
python3 bench.py 100000 120                # 10 万连接 / 模拟 120 秒
```

## 计时重置规则

**重置（仅入站证据，证明对端存活）：**
- `RX_DATA`：收到对端应用数据
- `RX_PROBE_ACK`：收到探测应答

**不重置：**
- `TX_DATA`：本端发送成功只说明本地协议栈未报错，数据可能仅进入发送
  缓冲，对端未必可达——这正是单向半开情形，故一律不重置
- `TX_PROBE`：本端发出的探测包，自己说话不能证明对端活着
- `TICK`：定时器推进
- 其他任何事件

重置动作：`last_activity = now`，`probes_sent = 0`，状态回到 `ESTABLISHED`。
`DEAD` 为终态，任何事件/tick 不再改变（判死后必须重建连接）。

## 探测与断开阈值（确定规则）

参数：`idle_timeout=30s`，`probe_interval=5s`，`max_probes=3`（均可配置）。

1. `now - last_activity >= idle_timeout` → 进入 `PROBING`，发探测 #1
2. 之后每满 `probe_interval` 无入站 → 发下一个探测
3. 已发满 `max_probes` 个探测且再满一个 `probe_interval` 仍无入站 → `DEAD`

**判死时间（自最后入站事件起）：`T_dead = idle_timeout + max_probes × probe_interval = 45s`**
——半开连接（对端静默消失）保证在 `max_probes` 轮探测内被识别。

边界（tie-break）规则：
- 所有比较均为 `>=`（左闭）：`now - last_activity == idle_timeout` 恰好相等
  立即进入探测；探测间隔恰好相等立即推进/判死
- 同一时刻既有事件又有 tick：**事件优先**（宁可晚杀一个 tick，不可误杀）
- tick 为追赶式：探测时刻锚定在 `last_activity + idle_timeout + k×interval`，
  判定结果与 tick 调用粒度无关（逐秒轮询 ≡ 一次性快进）

## 事件序列 → 判定结果（完整数据见 sequences.json）

### 场景 1：有数据往来（入站间隔 29s < 30s）
```
t=0..120，每 29s 一个 RX_DATA，奇数秒穿插 TX_DATA
→ 全程 ESTABLISHED，probes_sent=0，永不误判
```

### 场景 2：纯空闲（无任何事件）
```
t=0..29   ESTABLISHED  probes=0
t=30      PROBING      probes=1   (30-0 >= 30，恰好卡阈值)
t=35      PROBING      probes=2
t=40      PROBING      probes=3
t=45      DEAD         probes=3   (45 = 30 + 3×5)
```

### 场景 3：单向半开（对端 t=0 后静默消失，本端每秒 TX_DATA）
```
与场景 2 逐秒完全相同：TX 不重置计时
t=30 PROBING(1) → t=35 PROBING(2) → t=40 PROBING(3) → t=45 DEAD
→ 在 max_probes=3 轮探测内识别，T_dead=45s
```

### 场景 4：探测正好卡在阈值 + 应答卡判死时刻
```
t=0..44  纯静默 → t=44 时 probes=3（30/35/40 各发一个）
t=45     RX_PROBE_ACK 与判死同一时刻到达
         事件优先 → ESTABLISHED, probes=0（连接存活）
对照（见测试）：若应答迟至 t=45.001，t=45 的 tick 已判 DEAD，终态不复活
```

## 对拍（引擎 vs 独立参考模型）

`test_keepalive.py` 中的 `reference()` 是与状态机引擎**独立编写**的参考
模型，直接按「距最后入站事件的静默时长」计算应有状态。对以下场景逐
tick 对拍，断言完全一致（`gen_sequences.py` 生成数据时再次校验）：

- 对端不回包（纯静默 / 只 TX 不 RX）
- 对端回包（突发入站，固定种子伪随机模式，200 tick）
- 探测阶段中途收到应答

## 性能与内存（10 万连接，实测）

工作负载：70% 活跃（每 10s 入站）+ 20% 半开（只 TX）+ 10% 纯空闲，
模拟 120 秒，每秒一次全量 `tick_many`：

| 指标 | 数值 |
|---|---|
| 每秒判定耗时 mean | **2.30 ms**（p50 2.05 ms，max 3.68 ms） |
| 判定吞吐 | ≈ 4350 万连接/秒 |
| 单连接对象 | 64 B（`__slots__`） |
| 单连接内存上界 | **80 B**（对象 + 列表指针 + 8B 余量） |
| 10 万连接内存上界 | **7.63 MB**（实测 RSS 增量 7.07 MB） |
| 结束时 DEAD 数 | 30,000（= 20% 半开 + 10% 空闲，符合预期） |

内存上界推导：`Connection` 仅 4 个 `__slots__` 字段（状态枚举引用 +
2 个 float + 1 个 int），无 `__dict__`；总上界 = `N × 80 B`，与连接数
严格线性，不随时间增长（无队列、无历史记录）。

## 边界用例清单（test_keepalive.py）

- `test_idle_exactly_at_timeout`：`now-last == idle_timeout` 恰好相等 → 立即探测
- `test_probe_intervals_exact`：探测时刻 30/35/40 精确卡阈值，t=45 判死
- `test_ack_arrives_exactly_at_deadline`：应答与判死同刻 → 事件优先，存活
- `test_ack_one_instant_late`：应答迟 1ms → 已 DEAD，终态不复活
- `test_ack_just_before_deadline`：应答早 1ms → 存活
- `test_coarse_ticks_fast_forward`：稀疏 tick 快进与逐秒轮询结果一致
- `test_tx_data_does_not_reset` / `test_one_way_half_open_detected_in_n_rounds`：
  单向半开 45s 内判死
- `test_own_probe_does_not_reset`：自身探测不重置
- `test_reset_event_set_exact`：重置事件集合精确等于 {RX_DATA, RX_PROBE_ACK}
- `test_stays_dead`：DEAD 后 tick 幂等

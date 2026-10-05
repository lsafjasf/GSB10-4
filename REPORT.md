# 断线重连状态重建：续传 vs 全量重放

语言：Python 3（仅标准库）。核心库 `reconnect/core.py`，自测 `tests/test_reconnect.py`。

## 判定依据

客户端断线前最后提交的 checkpoint 携带三要素，重连时由服务端 `Server.decide()` 校验：

| 要素 | 含义 | 来源/维护 |
|---|---|---|
| `version` | 状态基线版本号 | 服务端整体更换状态来源（换快照/重建）时 +1，旧版本位点与摘要全部作废 |
| `seq` | 位点：已应用到的最大事件序号 | 每条事件单调递增；服务端保留 `(base_seq, latest_seq]` 区间的事件与逐位点摘要 |
| `digest` | 该位点处的状态摘要 | 逐键哈希的 XOR 累积值，单事件 O(1) 增量更新，与键枚举顺序无关 |

## 判定表

按优先级自上而下，命中即返回；任何依据不可用/不一致/过期都安全退化为全量，**绝不猜着续**：

| # | 条件 | 判定 | reason |
|---|---|---|---|
| 1 | `version < 0`（客户端无 checkpoint） | 全量 | `initial-sync` |
| 2 | `version ≠ server.version`（基线已更换） | 全量 | `version-mismatch` |
| 3 | `seq > latest_seq`（位点比服务端还新，连错源/数据异常） | 全量 | `seq-ahead-of-server` |
| 4 | `seq < base_seq`（位点已被日志截断，过期） | 全量 | `seq-expired` |
| 5 | 该 `seq` 无摘要记录（依据不可用，如崩溃丢失） | 全量 | `digest-unavailable` |
| 6 | `digest ≠ 服务端同位点摘要`（状态已分叉） | 全量 | `digest-mismatch` |
| 7 | 以上全部通过 | **续传**，补发 `(seq, latest_seq]` | `ok` |

续传的不重不漏由两道机制保证：

1. **原子提交**：补发事件先落在暂存区，整批到齐才更新 `state` 与 checkpoint；
   传输途中再次断开则部分批次直接丢弃，checkpoint 原地不动，下次重连安全重试。
2. **端到端摘要校验**：整批应用完后客户端摘要必须等于服务端当前摘要，
   不等即抛 `StateCorrupted`（丢/重一定会暴露），提交后位点才前进。

`decide()` 是纯函数：同一 checkpoint 反复/交错重连得到同一判定，天然幂等。

## 对拍数据（续传 vs 全量重放）

`python3 demo/diff_data.py` 生成 `data/diff_data.csv`（30 个用例：9 个定向 + 21 个随机），
逐用例比对续传路径最终摘要与全新客户端全量重放摘要，**30/30 一致**。节选：

```
case                        decision               resumed  equal  digest(16)
normal-resume               ok                         100  True   1c870939e0cf494b
normal-resume-small-gap     ok                           1  True   b3042e4705231ae2
normal-resume-zero-gap      ok                           0  True   84b798b9a7a259ad
drop-mid-batch-once         ok                          80  True   558366c91e0dd113
drop-mid-batch-4-times      ok                         200  True   2805fbc2319d22e2
forced-version-mismatch     version-mismatch             0  True   9a8d9caf7af213a2
forced-seq-expired          seq-expired                  0  True   47044fd6b0d514ce
forced-digest-unavailable   digest-unavailable           0  True   c9ae46219fb06174
forced-digest-mismatch      digest-mismatch              0  True   04eb8c2f51d8ff99
... (16 more fuzz rows in CSV, all equal=True)
```

另有 `TestResumeVsFullFuzz`：200 轮随机事件流 × 随机断连/截断/版本更替/摘要丢失，
每轮续传结果都与全量重放对拍，全部一致。

## 状态轨迹（反复重连 + 交错重连）

`python3 demo/trace_demo.py` 实际输出：

```
step action                            decision                  cli.seq cli.digest  cli.state
--------------------------------------------------------------------------------------------------------------
1    server: 5 events; client connect #1FULL    (initial-sync)    5       6edb616c82  {'a': 5}
2    server: +6 events; client reconnect #2RESUME  +6 events      11      611d5d1c75  {'a': 8, 'b': 2}
3    reconnect #3: 15 events, dropped  DROP    4/15 delivered    11      611d5d1c75  {'a': 8, 'b': 2}  (未提交, 位点不动)
4    reconnect #4: retry               RESUME  +15 events        26      4ce0428faa  {'a': 38, 'b': 2}
5    server: compact(keep 5); reconn#5 FULL    (seq-expired)      46      4d9f6b4f52  {'a': 38, 'b': 2, 'c': 20}
6    server: rotate_version; reconn #6 FULL    (version-mismatch) 48      9a17596b4b  {'a': 38, 'b': 2, 'c': 20, 'mode': 'v2'}
  ... other client first connect: FULL    (initial-sync), state == server: True
7    reconnect #7 (interleaved)        RESUME  +4 events         52      898765d177  {'a': 42, 'b': 2, 'c': 20, 'mode': 'v2'}
  ... other client reconnect:     RESUME  +4 events, state == server: True
--------------------------------------------------------------------------------------------------------------
final: client == server == other -> True
```

关键点：step 3 传输中断后 `cli.seq` 与 digest 与 step 2 完全相同（部分批次未污染状态）；
step 5/6 位点过期与版本更替都安全退化为全量；两个客户端交错重连互不影响，最终三方一致。

## 耗时与内存（10 万次重连）

`python3 bench/benchmark.py`（CPython 3.12，每次断连期间 1 条新事件，每 1000 次重连 compact 一次）：

```
reconnects          : 100,000
total time          : 0.601 s
avg per reconnect   : 6.01 us
throughput          : 166,285 reconnects/s
final state keys    : 256
client object bytes : 26,007 B (state 25,154 B + checkpoint 412 B)
tracemalloc peak    : 3,317,797 B (10k-reconnect 采样窗口)
state consistent    : True
```

内存上界：

- **客户端 O(K)**：K = 状态键数（本场景 256 键 ≈ 26 KB，其中 checkpoint 仅 412 B），
  与重连次数、历史事件总量均无关。
- **服务端 O(R)**：R = 保留窗口内事件数（事件 + 逐位点摘要），`compact(keep_last)` 后有硬上界。
- 单次续传判定的额外开销 O(gap)：只补发缺口事件；定位用二分 O(log R)。

## 情形覆盖 → 测试映射（14 个测试全过）

| 要求情形 | 测试 |
|---|---|
| 正常续传（不重不漏 + 对拍） | `TestNormalResume`、`TestResumeVsFullFuzz` |
| 被迫全量（版本/位点超前/摘要错位/摘要丢失） | `TestForcedFullReplay` |
| 位点过期（含边界恰好可续） | `TestSeqExpired` |
| 重连期间再次断开（事件批/快照批均原子） | `TestDisconnectDuringReconnect` |
| 反复 + 交错重连 | `TestInterleavedReconnects` |
| 增量摘要正确性 | `TestIncrementalDigest` |

## 运行命令

```bash
python3 -m unittest discover -s tests -v   # 自测（14 个用例）
python3 demo/trace_demo.py                 # 重连序列状态轨迹
python3 demo/diff_data.py                  # 生成对拍数据 data/diff_data.csv
python3 bench/benchmark.py                 # 10 万次重连耗时与内存
```

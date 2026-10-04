# 断线重连后的状态重建 —— 设计与自测报告

语言：Python 3（仅标准库：`hashlib`/`collections`/`unittest`/`tracemalloc`/`resource`）。

## 1. 续传 vs 全量：判定依据

客户端重连时携带续传凭据 `ResumeToken(offset, version, digest)`，服务端按下列
顺序判定，**任一条不过即全量**（fail-safe，不猜着续）：

1. **位点保留**：服务端只保留有限回放窗口 `[base_seq+1, seq]`。
   - `offset` 必须满足 `base_seq <= offset <= seq`；
   - `offset < base_seq` → 位点过期（窗口已滑走，补不齐增量）；
   - `offset > seq` → 位点超前（凭据不可信）。
2. **版本号一致**：`token.version == server.version`。服务端重置 / 快照压缩 /
   换数据源后 `version+1`，跨世代的位点不可比较。
3. **状态摘要吻合**：digest 是状态字典的顺序无关增量哈希
   （`digest = Σ blake2b-64(key,value) mod 2^64`），客户端在 `offset` 处的
   摘要必须等于服务端在**同一 offset** 处保存的摘要；不一致说明本地状态被污染
   （丢更新、重复应用、本地写坏）。

### 判定表

| # | 凭据存在 | 版本一致 | 位点范围 | 摘要吻合 | 判定 | 原因 |
|---|---------|---------|---------|---------|------|------|
| 1 | 否（首次连接） | — | — | — | **全量** | 无续传凭据 |
| 2 | 是 | 否 | — | — | **全量** | 跨世代位点不可比较 |
| 3 | 是 | 是 | `offset > seq`（超前） | — | **全量** | 凭据不可信 |
| 4 | 是 | 是 | `offset < base_seq`（过期） | — | **全量** | 所需事件已超出回放窗口 |
| 5 | 是 | 是 | 窗口内 | 服务端无法给出摘要 | **全量** | 依据不可用 |
| 6 | 是 | 是 | 窗口内 | 否 | **全量** | 本地状态已被污染 |
| 7 | 是 | 是 | 窗口内 | 是 | **续传** | 三项依据全部成立 |

续传路径还有客户端侧硬校验保证**不重不漏**：收到的每条事件 `seq` 必须
恰好等于 `本地 offset+1`，否则抛 `ProtocolError`；已应用事件不回滚，再次
断开后从已推进的新位点重连。全量路径采用"快照整体替换 + 快照之后的增量"，
绝不在旧状态上叠加。

## 2. 续传与全量对拍数据

`test_resume_vs_full_differential`：同一个事件源上跑两个客户端——
A 按真实判定续传（且每轮按随机 `drop_after` 抖动断连），B 每轮丢弃凭据强制
全量。60 轮、共 18,500 条随机 set/del 事件，每轮断言三方相等：
A 状态 == B 状态 == 服务端权威状态，摘要也相等。摘录（完整数据
见 `differential.csv`，由测试自动生成）：

| round | server_seq | resume_digest | full_digest | 状态相等 |
|------:|-----------:|---------------|-------------|:-------:|
| 1 | 800 | e54d862fb5979af7 | e54d862fb5979af7 | True |
| 2 | 1100 | 625f8e842f0c85e7 | 625f8e842f0c85e7 | True |
| 4 | 1700 | c9c0504400cb6e03 | c9c0504400cb6e03 | True |
| 8 | 2900 | c1d3d2865a73da17 | c1d3d2865a73da17 | True |
| 20 | 6500 | 278c387b7b7091f8 | 278c387b7b7091f8 | True |
| 40 | 12500 | 35a5482b0c76a8b4 | 35a5482b0c76a8b4 | True |
| 60 | 18500 | 262777f24e6ff5f4 | 262777f24e6ff5f4 | True |

续传与全量重放的最终状态逐字节一致（dict 相等 + 摘要相等），即续传
**不重不漏**。

## 3. 反复 / 交错重连的状态轨迹

`test_repeated_reconnect_trajectory`：7 轮脚本，新增事件数与当次
`drop_after`（None=不断开）交错。每轮内反复重连直到追平，每轮结束断言与
服务端状态一致（完整轨迹见 `trajectory.txt`）：

| round | 路径 | 本轮连接尝试次数 | offset | digest@offset | 状态键数 |
|------:|:----:|---------------:|-------:|---------------|--------:|
| 1 | full | 1 | 0 | 0000000000000000 | 0 |
| 2 | resume | 7 | 40 | 0823b49288296aed | 22 |
| 3 | resume | 1 | 80 | 14b6b42dc83260f9 | 36 |
| 4 | resume | 4 | 120 | c58c7f62ba7c6eb8 | 42 |
| 5 | resume | 1 | 160 | f9cfc62eeabaedc8 | 36 |
| 6 | resume | 10 | 200 | fa9708a800fdc7ca | 42 |
| 7 | resume | 1 | 240 | 0d1387268b1b0eec | 43 |

要点：首次全量；之后全部续传；断连次数再多，offset 单调推进，同一事件不
会被应用两次（`seq == offset+1` 硬校验），digest 随状态单调收敛且每轮
等于服务端摘要。另有 `test_drop_during_resume_and_interleaved_drops`
覆盖 `drop_after = [3,1,7,None,2,None,5,1,None]` 的交错序列，以及
`test_drop_then_event_trimmed_forces_renegotiation`：续传中所需事件被
服务端清理 → 当次连接失败 → 下次重连安全退化为全量。

## 4. 10 万次重连：耗时与内存上界

`bench_reconnect.py`：每轮发布 3 条事件后断连重连，共 10 万次重连、
30 万条事件，key 空间 1 万，回放窗口保证全程可续传。实测（本机，
Python 3.12）：

- 状态重建（判定 + 握手 + 续传 3 条增量事件）总耗时 **3.07 s**，
  单次重连 **30.7 µs**，约 **32,500 次重连/s**；
  含事件生产在内整轮 6.5 s。
- 10 万次续传后终检通过：客户端状态 == 服务端状态 == 独立全量重算摘要。
- 内存（递归 sizeof，对象图）：
  - 状态对象 dict（10,000 keys）：**957,616 B ≈ 935.2 KiB**；
  - `StateStore`（含 digest、offset 等 O(1) 附加）：957,664 B；
  - `Client` 整体：957,712 B ≈ 935.3 KiB。
- 进程级：`tracemalloc` 峰值 70.3 MiB、峰值 RSS 235.9 MiB（主要是服务端
  回放窗口：retention=2,000,000 的事件与摘要日志，属服务端 O(retention)
  开销，不在客户端状态上界内）。

**内存上界结论**：客户端状态内存 = `O(不同 key 数)`，与重连次数无关；
本场景上界 10,000 keys → 935.2 KiB，摘要与位点占 O(1)。服务端回放窗口
内存 = O(retention)。

## 5. 覆盖情形与运行命令

| 情形 | 测试 |
|------|------|
| 正常续传（不重不漏） | `test_normal_resume_no_gap_no_duplicate` |
| 被迫全量：无凭据 / 版本不一致 / 摘要不符 / 位点超前 | `test_full_when_*`（4 个） |
| 位点过期 | `test_full_when_offset_expired` |
| 重连期间再次断开、交错断连 | `test_drop_during_resume_and_interleaved_drops` |
| 续传中事件被清理 → 重新协商全量 | `test_drop_then_event_trimmed_forces_renegotiation` |
| 反复重连状态轨迹 | `test_repeated_reconnect_trajectory` |
| 续传 vs 全量对拍 | `test_resume_vs_full_differential` |
| 增量摘要正确性 | `test_digest_incremental_matches_full_recompute` |

```bash
cd reconnect
python3 -m unittest -v test_reconnect   # 11 个用例，自动生成 differential.csv / trajectory.txt
python3 bench_reconnect.py              # 10 万次重连压测（可传参：python3 bench_reconnect.py 50000）
```

## 6. 文件清单

- `reconnect_lib.py`：库（`Server`/`Session`/`Client`/`StateStore` + 判定逻辑）
- `test_reconnect.py`：11 个自测用例
- `bench_reconnect.py`：10 万次重连耗时 / 内存压测
- `differential.csv`、`trajectory.txt`：测试生成的对拍数据与状态轨迹

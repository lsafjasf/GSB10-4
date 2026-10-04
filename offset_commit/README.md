# 消费位点批量提交与回退（Python 3，仅标准库）

## 位点规则说明

位点为严格递增的非负整数 `0,1,2,...`。`committed` 表示"已连续提交"的最高位点，初始 `-1`。

1. **完成不等于提交**：处理完成只把位点放进 `pending`（已完成未提交集合）。
2. **连续前缀推进**：`advance()` 从 `committed+1` 开始，只要下一位点在 `pending` 中就连续提交；
   **遇到空洞立即停止，空洞之后的位点绝不提前提交**（否则重启后这些位点会被跳过，丢消息）。
3. **批量提交** = `mark_done(一批)` + `advance()`，与逐条提交的最终 `committed` 严格一致（见对拍）。
4. **失败回退**：`BatchGuard.rollback()` 只撤销本批"已标记完成但尚未提交"的位点，范围最小；
   已进入连续前缀的位点不可在位点层回退（需上层补偿），正常解耦流程下不会出现。
5. **处理与提交解耦**：处理方只写 WAL `D <off>`；提交方 `advance()` 后写 `C <off>`。
   进程被 `kill -9` 后重启，`recover()` 重放 WAL：`C` 之前视为已提交，`C` 之后有 `D` 记录的
   进入 `pending` 并补齐连续前缀，没有 `D` 记录的位点重新处理 —— **不跳过任何未处理位点**
   （at-least-once，可能重放但绝不丢消息）。撕裂的半行日志会被丢弃。

## 文件

- `offset_commit.py` — 库：`OffsetTracker` / `BatchGuard` / `OffsetStore`(WAL)
- `worker.py` — 崩溃测试用处理进程（处理→DONE 日志，独立步骤推进→COMMIT 日志）
- `test_offset_commit.py` — 自测：严格递增 / 乱序 / 空洞 / 失败回退 / kill -9 重启 / 对拍 / 100 万耗时
- `demo.py` — 打印各场景位点序列与回退数据
- `RESULTS.md` — 实测数据（对拍、崩溃、耗时、内存）

## 运行命令

```bash
cd offset_commit
python3 test_offset_commit.py   # 全部自测（含对拍/崩溃/100万耗时）
python3 demo.py                 # 打印位点序列与回退数据
```

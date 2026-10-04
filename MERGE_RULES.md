# submerge — 字幕合并与偏移规则说明

纯 Python 3 标准库实现，无第三方依赖。时间轴内部用整数毫秒表示，
每条字幕是**半开区间 `[start, end)`**（含头不含尾）。

## 合并规则（merge）

1. 输入可为一个或多个字幕列表/文件，全部汇到一起。
2. **零时长/负时长条目**（`end <= start`）区间为空，直接丢弃并逐条报告
   （`MergeResult.dropped` / CLI stderr 警告），绝不就地修补。
3. 剩余条目按 `(start, end, 来源顺序)` 排序后线性扫描：
   下一条的 `start` 小于当前输出条的 `end` 即判定重叠，并入当前条。
4. 合并是**传递闭包**：A 与 B 重叠、B 与 C 重叠 ⇒ A、B、C 合成一条，
   即使 A 与 C 本身不相交。完全嵌套（被包含）的条目同样被吸收。
5. 合成条区间取 `[min start, max end)`，文本按扫描顺序用换行拼接。
6. 相邻但端点相接的条目（`[1s,2s)` 与 `[2s,3s)`）**不算重叠**，保持独立。

保证：输出按开始时间升序、两两不重叠、无嵌套、无零时长条目。

## 偏移规则（shift）

- **整段平移** `shift_cues(delta)`：所有条目加同一个 `delta`（毫秒，可为负），
  条目间距与重叠关系完全不变。
- **分段平移** `shift_by_segments(segments)`：每个段是半开区间
  `[start, end)` 加自己的 `delta`；条目按**其开始时间落在哪个段**决定平移量，
  不在任何段内的条目不动。段与段不得互相重叠（会校验报错）。
- **越界处理**：任何条目平移后 `start < 0` 即越界。收集**全部**违例条目，
  抛出 `OutOfBoundsError`（CLI 退出码 2 并打印报告，见
  `fixtures/oob_report.txt`），**不截断、不写出任何结果**。恰好平移到 0 合法。
- 偏移前会校验输入无零时长条目（否则报错），因此偏移不可能产生零时长条目。

### 两种平移的差异

| | 整段平移 | 分段平移 |
|---|---|---|
| 条目间距 | 完全保持 | 段边界两侧的间距会改变 |
| 重叠关系 | 不变 | 可能新产生重叠（段被压缩靠拢时） |
| 越界风险 | 全体一致 | 只有负 delta 段内的条目有风险 |
| 后续处理 | 无需再合并 | **必须再跑一次 merge** 消除新重叠 |

示例（`fixtures/case_shift_in.srt`：A=[1s,2s)，B=[5s,6s)）：
整段 `-3s` 会因 A 越界被整体拒绝；分段 `[4s,8s) → -3s` 只移动 B，
得到 A=[1s,2s)、B=[2s,3s)，成功。CLI 的 `shift` 在分段平移后自动再合并，
见 `fixtures/case_segoverlap_in.srt` → `expected/case_segoverlap_merged.srt`。

## 对拍数据（手工推算 vs 库输出）

`fixtures/` 下每个 case 的期望输出均为逐条手工推算，
`test_submerge.py::TestCrossCheck` 断言库输出与期望文件**逐字节一致**：

| 用例 | 输入 | 期望 | 手工推算要点 |
|---|---|---|---|
| 基础重叠+端点相接 | `case_overlap_a/b.srt` | `case_overlap_merged.srt` | [1,3)∩[2,4)→[1,4)；4s、5s 相接不并 |
| 传递链+嵌套 | `case_chain_a/b.srt` | `case_chain_merged.srt` | A~B~C 三点成链；BIG 吞掉 NESTED |
| 零时长丢弃 | `case_zero_a.srt` | `case_zero_merged.srt` | [1s,1s) 丢弃并报告 |
| 空字幕 | `case_empty.srt` | `case_empty_merged.srt` | 空输入→空输出 |
| 整段平移 | `case_shift_in.srt` +2.5s | `case_shift_plus2500.srt` | 全部 +2500ms |
| 分段平移 | `case_shift_in.srt` 段[4s,8s)/-3s | `case_shift_segments.srt` | 仅 B 移动 |
| 分段后重叠再合并 | `case_segoverlap_in.srt` 段[2.5s,5s)/-2s | `case_segoverlap_merged.srt` | B 移到 [1,2) 与 A 重合 |
| 越界报告 | `case_oob_in.srt` -10s | `oob_report.txt`（退出码 2） | 列出违例、不写文件 |

## 边界用例覆盖（test_submerge.py，17 项）

空字幕、完全重叠/嵌套、传递链、端点相接不并、零时长（合并丢弃 /
偏移拒绝）、偏移后越过零点（报错且不截断）、恰好平移到 0、
段区间半开边界、段互相重叠拒绝、整段 vs 分段差异、合并幂等性、
输出有序且不重叠（2000 条随机断言）、文本拼接顺序。

## 性能（5 万条）

测试机 Python 3.12，两份各 2.5 万条、刻意制造大量重叠（随机种子 42）：

- 合并 50,000 → 1,367 条，耗时约 **170–190 ms**
- Python 堆内存峰值约 **9.4 MiB**（tracemalloc）

## 运行命令

```bash
# 自测（含对拍与基准）
python3 test_submerge.py -v

# 合并
python3 submerge.py merge out.srt a.srt b.srt [c.srt ...]

# 整段平移（秒，可为负，也接受 HH:MM:SS,mmm）
python3 submerge.py shift out.srt in.srt --delta -2.5

# 分段平移：start,end,delta 三元组，分号分隔；平移后自动再合并
python3 submerge.py shift out.srt in.srt \
  --segments "00:00:04,000,00:00:08,000,-3; 00:10:00,000,00:20:00,000,1.5"
```

退出码：0 成功；1 输入/参数错误；2 偏移越界（报告打到 stderr，不写输出文件）。

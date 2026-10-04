# 持久化与重启恢复

## 磁盘布局（数据目录内）

- `wal.log` — 追加式 WAL，一行一条 JSON 记录（JSON 字符串内换行会被转义，
  故一条记录恰占一行）。首行为头 `{"magic":"delayq-wal","version":1}`。
  记录类型：
  - `{"ty":"SCHED","seq":…,"id":…,"due":…,"payload":…}`
  - `{"ty":"CANCEL","id":…}`
  - `{"ty":"DELIVER","seq":…}`
- `snapshot.jsonl` — 压缩快照：头行含 `next_seq`，其后按 `(due, seq)` 升序
  排列的全部挂账 SCHED 记录（已投递/已取消的不入快照）。
- `*.tmp` — 快照/WAL 替换用的临时文件，写完后 `os.replace` 原子改名。

## 写入路径

`schedule` / `cancel` / `pump` 每产生一条记录都执行
`write + flush + fsync`（`durable=True` 默认）。顺序：**先 fsync 持久化，
再改内存语义生效**（pump 中 DELIVER 记录 fsync 后才调用用户 sink）。

## 恢复流程（`DelayedQueue.__init__`）

1. 若存在 `snapshot.jsonl`：读头恢复 `next_seq`，按序把挂账消息插入堆。
2. 顺序回放 `wal.log`：
   - `SCHED`：若该 `seq` 已存在（来自快照）则跳过——回放按 seq 幂等，
     因此「快照已替换、WAL 尚未截断」的崩溃窗口也安全；
   - `CANCEL`：从挂账中删除对应 id；
   - `DELIVER`：从挂账中删除，并把 id 记入「已投递集合」；
   - 无换行结尾的尾行视为崩溃中的半条写，直接忽略。
3. 打开 WAL 继续追加（不存在则新建并写头）。

恢复后堆、两个索引 dict、`next_seq`、已投递集合与崩溃前完全一致，故到期
顺序与未重启时逐字节一致（对拍见 `SEQUENCING.md`）。

## 快照（`snapshot()`）

1. 把挂账消息按 `(due, seq)` 排序写入 `snapshot.jsonl.tmp`，fsync，
   `os.replace` 原子替换旧快照，fsync 目录；
2. 用只含头行的新 WAL 原子替换旧 WAL；
3. 清空「已投递集合」。

任意时刻崩溃都安全：旧快照+旧 WAL、新快照+旧 WAL（幂等去重）、
新快照+新 WAL 三种组合都能恢复到一致状态。

## 取消的确定语义与保留边界

- 未到期取消 → `CANCELLED`，写 CANCEL 记录，堆条目惰性删除；
- 已投递取消 → `ALREADY_DELIVERED`（DELIVER 记录仍在当前 WAL 代内即可判定，
  跨重启同样成立）；
- 不存在 → `NOT_FOUND`；
- **保留边界**：`snapshot()` 只保留挂账消息，已投递/已取消 id 被丢弃
  （否则内存无界增长）。快照之后再取消一个快照前已投递的 id 返回
  `NOT_FOUND`。这是显式文档化的边界，测试
  `test_cancel_after_snapshot_is_not_found_boundary` 锁定该行为。

## 崩溃窗口与投递保证

DELIVER 记录先 fsync、后调 sink：崩溃发生在两者之间时，该消息恢复后
**不会**重复投递。即队列保证状态转移恰好一次、sink 至多一次
（at-most-once）。需要 sink 至少一次语义时，应把 sink 做成幂等并在
sink 成功后再做应用侧确认（本库刻意保持简单，未实现两阶段提交）。

## 时间注入

库内不读任何时钟。`pump(now, sink)` 的 `now` 由调用方注入；测试用虚拟
时钟推进，生产侧通常 `pump(time.time(), sink)` 周期调用。

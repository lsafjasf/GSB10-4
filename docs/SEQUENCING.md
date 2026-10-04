# 顺序规则说明

## 规则

1. 每次 `schedule(id, due, payload)` 分配一个进程内全局单调递增序号 `seq`
   （从 0 开始，重启后从 WAL/快照中的最大 seq 继续，不回卷）。
2. 投递键为 `(due_time, seq)`，用最小堆维护。`pump(now)` 不断弹出堆顶，
   直到堆顶 `due > now`。
3. 因此：
   - 不同到期时间：早到期先投递；
   - 同一到期时间：**严格按入队顺序**（seq 小的先投递）。
   该次序与 Python dict 顺序、对象地址、随机种子均无关，是确定的。
4. `pump` 内部投递序列示例（见 `tests` 的
   `test_ties_follow_enqueue_seq_even_with_interleaving`）：

   ```
   schedule(t1, due=50); schedule(t2, due=10);
   schedule(t3, due=50); schedule(t4, due=10); schedule(t5, due=50)
   pump(50) -> [t2, t4, t1, t3, t5]
   ```

## 取消后的次序

取消只从两个 dict 中移除挂账条目（`_by_seq` / `_id_to_seq`），堆中的条目
成为“废堆顶”，由后续 `pump` 弹出时跳过。废条目不参与投递，也不影响其余
消息的相对次序。

## 重启后的次序一致性

`seq` 与全部挂账消息都持久化（见 `PERSISTENCE.md`）。重启恢复后：

- 堆由相同的 `(due, seq)` 键重建；
- `seq` 计数器恢复到 `max_seq + 1`，新消息的序号不会与历史序号冲突；
- 已 CANCEL / 已 DELIVER 的消息不会重新进入堆。

所以「重启后从 t 时刻开始的投递序列」与「从未重启时从 t 时刻开始的投递
序列」完全一致。`scripts/restart_crosscheck.py` 用 4001 个确定性操作（含
立即到期、逾期补录、取消、对未知 id 取消、多次 pump、多次快照）对拍：
连续运行轨迹与「每条操作后关闭重开」轨迹逐字节相同，存档见
`docs/data/crosscheck_*.trace`（md5 相同）。

## 对拍数据

- `docs/data/crosscheck_scenario.txt`：场景（每个操作一行，随机种子
  20261005，可复现）。
- `docs/data/crosscheck_continuous.trace`：连续运行轨迹（SCHED/CANCEL/PUMP/
  SNAPSHOT 结果）。
- `docs/data/crosscheck_restart.trace`：每条操作后重启的轨迹，与上行文件
  md5 相同。

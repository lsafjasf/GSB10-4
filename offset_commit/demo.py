"""打印各场景下的位点推进序列与回退数据（供核对）。"""

from offset_commit import BatchGuard, OffsetTracker

print("== 场景1：严格递增，逐条提交 ==")
t = OffsetTracker()
for off in range(6):
    _, advanced = t.commit_batch([off])
    print("  完成 %d -> 提交序列 %s, committed=%d" % (off, advanced, t.committed))

print("== 场景2：乱序完成，中间有洞 ==")
t = OffsetTracker()
for batch in ([5], [1, 3], [2], [0], [4]):
    _, advanced = t.commit_batch(batch)
    print("  完成 %-7s -> 提交序列 %-12s committed=%d pending=%s"
          % (batch, advanced, t.committed, sorted(t.pending)))
print("  说明：位点5在空洞0之前完成，始终留在 pending，绝不越过空洞提交。")

print("== 场景3：失败回退（范围最小） ==")
t = OffsetTracker()
t.commit_batch([0, 1, 2, 3])
guard = BatchGuard(t)
for off in (4, 5, 6):
    if off == 6:
        break
    guard.mark_done(off)
rolled, already = guard.rollback()
print("  批 [4,5,6] 处理到6失败；回退位点 %s；已提交无法回退 %s" % (rolled, already))
print("  回退后 committed=%d, is_done(4)=%s, advance()->%s" %
      (t.committed, t.is_done(4), t.advance()))
_, advanced = t.commit_batch([4, 5, 6])
print("  重试成功 -> 提交序列 %s, committed=%d" % (advanced, t.committed))

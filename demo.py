"""演示：中断点落在各阶段时的清理结果矩阵 + 已完成再中断的对拍。

运行：python3 demo.py
"""

from coop_timeout import Kind, ResourceLedger, make_failing_task, make_resource_task, run


def cleanup_matrix() -> None:
    print("== 清理结果矩阵：中断点落在各个阶段 ==")
    print(f"{'deadline':>9} {'终态':<9} {'中断阶段':<10} {'响应时刻':>8} "
          f"{'信号到期':>8} {'延迟':>5} {'释放序列':<10} {'遗留资源':<6} {'deferred':>8} {'late':>4}")
    rows = [
        ("入口即超时", 0.0, ()),
        ("获取A中", 0.5, ()),
        ("获取A后", 1.0, ()),
        ("获取B中", 2.0, ()),
        ("获取B后", 3.0, ()),
        ("工作中", 4.5, ()),
        ("提交边界(平局)", 7.0, ()),
        ("刚好错过", 7.0001, ()),
        ("清理中再取消", 4.5, ((6.5, "again"),)),
        ("主动取消", None, ((4.5, "user"),)),
        ("永不超时", None, ()),
    ]
    for label, deadline, cancels in rows:
        ledger = ResourceLedger()
        res = run(make_resource_task(ledger), deadline=deadline, cancels=cancels)
        o = res.outcome
        print(f"{label:>9} {o.kind.value:<9} {o.phase or '-':<10} "
              f"{_fmt(o.responded_at):>8} {_fmt(o.signal_at):>8} {_fmt(o.latency):>5} "
              f"{str(list(o.released)):<10} {str(ledger.held()):<6} {o.deferred:>8} {o.late:>4}")

    print()
    print("== 自身失败（与中断无关，同样完整清理） ==")
    ledger = ResourceLedger()
    o = run(make_failing_task(ledger)).outcome
    print(f"kind={o.kind.value} error={o.error!r} phase={o.phase} "
          f"released={list(o.released)} held={ledger.held()}")


def late_interrupt_crosscheck() -> None:
    print()
    print("== 已完成再中断：结果不变（对拍） ==")
    ledger = ResourceLedger()
    res = run(make_resource_task(ledger))
    before = res.outcome.fingerprint()
    print(f"完成时指纹: {before}")

    res.token.cancel()                       # 完成后立即取消
    res.token.cancel(at=res.clock.now + 50)  # 再排一个未来取消
    res.clock.advance(50.0)
    res.clock.pump()

    after = res.outcome.fingerprint()
    print(f"中断后指纹: {after}")
    print(f"指纹一致: {before == after}  kind={res.outcome.kind.value} "
          f"value={res.outcome.value} 观测到的迟到信号 late={res.token.late}")


def _fmt(v):
    return "-" if v is None else f"{v:g}"


if __name__ == "__main__":
    cleanup_matrix()
    late_interrupt_crosscheck()

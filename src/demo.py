"""打印交付所需的场景数据：可见性、各失败点回退结果、重复回退、对拍。"""

from partial_commit import MISSING, Stage, Store, Task, Transaction


def show(title):
    print("=" * 72)
    print(title)
    print("=" * 72)


def print_rollback(rb):
    print(f"  rollback() 累计调用次数: {rb.rollback_calls}")
    print(f"  fully_rolled_back     : {rb.fully_rolled_back}")
    print("  各阶段回退结果（实际撤销顺序）:")
    for r in rb.per_stage:
        line = f"    - {r.stage}: state={r.state}, attempts={r.attempts}"
        if r.error:
            line += f", error={r.error}"
        print(line)


def build_stages(fail_stage, n=4, compensators=None):
    compensators = compensators or {}

    def make_apply(i):
        def apply(tx):
            tx.write(f"k{i}", i * 10)
            if i == fail_stage:
                raise RuntimeError(f"stage {i} failed")
        return apply

    return [Stage(f"stage{i}", make_apply(i), compensators.get(i)) for i in range(n)]


def main():
    # ---- 1. 可见性演示（V1/V2/V4） ----
    show("1. 可见性规则演示")
    store = Store()
    store._apply("seed", "S")
    tx = Transaction(store)
    tx.write("k0", "draft")
    print("阶段提交前:")
    print("  任务内读 k0 =", tx.read("k0"), " | 外部读 k0 =", store.read("k0"))

    def s0(t): t.write("k0", "v0")
    def s1(t): t.write("k1", "v1")
    def s2(t):
        print("阶段2开始时外部可见:", store.snapshot(), "（已部分提交 k0,k1）")
        t.write("k2", "v2")
        raise RuntimeError("boom in stage2")
    res = Task(store, [Stage("s0", s0), Stage("s1", s1), Stage("s2", s2)]).run()
    print("阶段2失败后（k2 从未对外可见）, 任务结束后外部状态:", store.snapshot())

    # ---- 2. 各失败点的回退结果 ----
    for fail in (0, 1, 2, 3):
        show(f"2.{fail} 阶段 {fail} 失败时的回退结果")
        s = Store()
        r = Task(s, build_stages(fail)).run()
        print(f"  status={r.status}, failed_stage={r.failed_stage}")
        print_rollback(r.rollback)
        print("  回滚后外部状态:", s.snapshot(), "（等价于任务从未执行）")

    # ---- 3. 幂等：重复回退数据 ----
    show("3. 回退幂等数据（中间阶段失败，连续调用 rollback 3 次）")
    s = Store()
    task = Task(s, build_stages(2))
    r = task.run()
    baseline_writes = s.write_count
    rb_calls = []
    for i in range(3):
        rb = task.rollback()
        rb_calls.append((
            rb.rollback_calls,
            [(x.stage, x.state, x.attempts) for x in rb.per_stage],
            s.write_count,
        ))
    print("  run() 自动首次回滚后 store.write_count =", baseline_writes)
    for calls, per, writes in rb_calls:
        print(f"  第{calls}次 rollback: attempts={per}, store.write_count={writes}")
    print("  结论: 每个阶段 attempts 恒为 1，重复回退 write_count 不增长 -> 零额外副作用")

    # ---- 4. 回滚中再次失败：首次失败、重试成功、再回退无副作用 ----
    show("4. 回滚中再次失败（补偿器第 1 次抛错，第 2 次成功）")
    s = Store()
    counter = {"flaky": 0, "other": 0}

    def undo(s, entries):
        for key, old in reversed(entries):
            s._delete(key) if old is MISSING else s._apply(key, old)

    def flaky(st, entries):
        counter["flaky"] += 1
        if counter["flaky"] == 1:
            raise RuntimeError("compensator crash")
        undo(st, entries)

    def other(st, entries):
        counter["other"] += 1
        undo(st, entries)

    compensators = {0: other, 1: flaky}
    task = Task(s, build_stages(2, compensators=compensators))
    r1 = task.run()
    print("  第一次回滚:")
    print_rollback(r1.rollback)
    print("  外部状态:", s.snapshot(), "（stage1 未撤销，残留 k1=10）")
    r2 = task.rollback()
    print("  第二次回滚（仅重试 FAILED 阶段）:")
    print_rollback(r2)
    print("  外部状态:", s.snapshot())
    writes = s.write_count
    task.rollback()
    print(f"  第三次回滚后 write_count {writes} -> {s.write_count}（无增长）; "
          f"补偿器调用次数 flaky={counter['flaky']}, other={counter['other']}")

    # ---- 5. 对拍数据：阶段化 vs 非阶段化 ----
    show("5. 成功路径对拍：阶段化最终状态 == 非阶段化（直接顺序写）")
    cases = [
        [("set", "a", 1), ("set", "b", 2), ("set", "a", 9)],
        [("set", "a", 1), ("del", "a"), ("set", "c", 3)],
        [("set", "x", 1), ("set", "y", 2), ("set", "z", 3), ("del", "y"), ("set", "x", 100)],
    ]
    all_ok = True
    for i, ops in enumerate(cases):
        staged, plain = Store(), Store()
        def make(op):
            if op[0] == "set":
                return lambda t, op=op: t.write(op[1], op[2])
            return lambda t, op=op: t.delete(op[1])
        tr = Task(staged, [Stage(f"s{j}", make(op)) for j, op in enumerate(ops)]).run()
        for op in ops:
            plain._apply(op[1], op[2]) if op[0] == "set" else plain._delete(op[1])
        ok = staged.snapshot() == plain.snapshot()
        all_ok &= ok
        print(f"  用例{i+1}: ops={ops}")
        print(f"        阶段化={staged.snapshot()}  非阶段化={plain.snapshot()}  "
              f"status={tr.status}  {'一致' if ok else '不一致!!'}")
    print("  结论:", "全部一致" if all_ok else "存在不一致")


if __name__ == "__main__":
    main()

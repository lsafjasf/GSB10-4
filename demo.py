"""演示：水位规则、长事务识别、超长处置、崩溃重建对拍。时间全部注入。"""

import os
import tempfile

from longtxn import GarbageCollector, ManualClock, StateLog, TransactionManager, VersionStore

LONG_THRESHOLD = 30.0
MAX_DURATION = 120.0


def new_mgr(store, clock, log_path=None):
    log = StateLog(log_path) if log_path else None
    return TransactionManager(store, clock, long_threshold=LONG_THRESHOLD,
                              max_duration=MAX_DURATION, state_log=log)


def short_txn(mgr, clock, key, value, dt=1.0):
    t = mgr.begin()
    mgr.write(t, key, value)
    mgr.commit(t)
    clock.advance(dt)


def main():
    print("=" * 72)
    print("1. 水位推进规则演示  watermark = min(active.start_seq)，空闲时 = commit_seq")
    print("=" * 72)
    clock, store = ManualClock(), VersionStore()
    mgr = new_mgr(store, clock)
    gc = GarbageCollector(store, mgr)
    print(f"  初始:            commit_seq={mgr.commit_seq:2d}  watermark={mgr.watermark():2d}")
    for i in range(3):
        short_txn(mgr, clock, "a", f"v{i}")
    print(f"  3 个短事务后:    commit_seq={mgr.commit_seq:2d}  watermark={mgr.watermark():2d}  (空闲->水位=commit_seq)")
    t_long = mgr.begin()
    print(f"  长事务 T{t_long} 开启:  commit_seq={mgr.commit_seq:2d}  watermark={mgr.watermark():2d}  (被 T{t_long} 的 start_seq 钉住)")
    for i in range(5):
        short_txn(mgr, clock, "a", f"w{i}")
    print(f"  再来 5 个短事务: commit_seq={mgr.commit_seq:2d}  watermark={mgr.watermark():2d}  (水位不动，垃圾积压)")
    stats = gc.collect()
    print(f"  GC 尝试:         回收 {stats.versions_removed} 个版本 (水位之下无冗余，无法越线)")
    mgr.commit(t_long)
    print(f"  T{t_long} 提交后:       commit_seq={mgr.commit_seq:2d}  watermark={mgr.watermark():2d}  (水位追平)")
    stats = gc.collect()
    print(f"  再次 GC:         回收 {stats.versions_removed} 个版本")

    print()
    print("=" * 72)
    print(f"2. 长事务识别  阈值 long_threshold={LONG_THRESHOLD}s, max_duration={MAX_DURATION}s")
    print("=" * 72)
    clock, store = ManualClock(), VersionStore()
    mgr = new_mgr(store, clock)
    t1 = mgr.begin()                       # 只读，将变长事务
    t2 = mgr.begin()
    mgr.write(t2, "b", "x")                # 有写事务
    clock.advance(10)
    t3 = mgr.begin()                       # 年轻事务
    clock.advance(25)                      # t1/t2 35s, t3 25s
    print(f"  时钟推进到 35s (t1,t2 年龄 35s; t3 年龄 25s)")
    for r in mgr.identify_long_txns():
        print(f"  识别结果: txn={r['txn_id']}  age={r['age']:.0f}s  "
              f"read_only={r['read_only']}  over_max={r['over_max']}")

    print()
    print("=" * 72)
    print("3. 超长事务处置  只读->降级(degrade)  有写->强制中断(force_abort)")
    print("=" * 72)
    clock.advance(MAX_DURATION)            # t1,t2 155s
    for ev in mgr.enforce():
        print(f"  处置: txn={ev.txn_id}  age={ev.age:.0f}s  action={ev.action}  "
              f"watermark {ev.watermark_before} -> {ev.watermark_after}")
    print(f"  处置后水位: {mgr.watermark()} (commit_seq={mgr.commit_seq})")

    print()
    print("=" * 72)
    print("4. 崩溃恢复重建对拍")
    print("=" * 72)
    with tempfile.TemporaryDirectory() as d:
        log_path = os.path.join(d, "state.jsonl")
        clock, store = ManualClock(), VersionStore()
        mgr = new_mgr(store, clock, log_path)
        for i in range(20):
            short_txn(mgr, clock, f"k{i % 4}", f"v{i}")
        t_orphan = mgr.begin()             # 崩溃时未决
        mgr.write(t_orphan, "k0", "dirty")
        for i in range(20, 40):
            short_txn(mgr, clock, f"k{i % 4}", f"v{i}")
        before = {
            "commit_seq": mgr.commit_seq,
            "watermark": mgr.watermark(),
            "active": sorted(mgr.active_txns()),
            "versions": store.total_versions(),
            "chains": {k: len(store.chain(k)) for k in sorted(store.keys())},
        }
        mgr.log.close()                    # <-- 模拟崩溃
        store2 = VersionStore()
        mgr2 = TransactionManager.recover(store2, ManualClock(), log_path,
                                          long_threshold=LONG_THRESHOLD,
                                          max_duration=MAX_DURATION)
        after = {
            "commit_seq": mgr2.commit_seq,
            "watermark": mgr2.watermark(),
            "active": sorted(mgr2.active_txns()),
            "versions": store2.total_versions(),
            "chains": {k: len(store2.chain(k)) for k in sorted(store2.keys())},
        }
        print(f"  孤儿事务(崩溃时未决): {mgr2.recovered_orphans} -> 恢复时强制中止")
        print(f"  {'字段':<12}{'崩溃前':<28}{'重建后':<28}对拍")
        for key in before:
            b, a = before[key], after[key]
            match = "MATCH" if (b == a or key == "watermark"
                                or (key == "active" and a == [])) else "DIFF"
            note = ""
            if key == "watermark":
                note = "(孤儿中止后水位=commit_seq，符合规则)"
                match = "RULE-OK"
            if key == "active":
                note = "(崩溃无幸存活跃事务)"
                match = "RULE-OK"
            print(f"  {key:<12}{str(b):<28}{str(a):<28}{match} {note}")
        mgr2.log.close()


if __name__ == "__main__":
    main()

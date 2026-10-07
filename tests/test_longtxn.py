"""长事务资源控制自测。时间全部由 ManualClock 注入。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from longtxn import (CrashInjected, GarbageCollector, ManualClock, StateLog,
                     TransactionManager, VersionStore)

LONG_THRESHOLD = 30.0
MAX_DURATION = 120.0


def make_env(log_path=None, **kw):
    clock = ManualClock()
    store = VersionStore()
    kw.setdefault("long_threshold", LONG_THRESHOLD)
    kw.setdefault("max_duration", MAX_DURATION)
    log = StateLog(log_path) if log_path else None
    mgr = TransactionManager(store, clock, state_log=log, **kw)
    gc = GarbageCollector(store, mgr)
    return clock, store, mgr, gc


def run_short_txns(mgr, clock, n, key_prefix="k", dt=0.001):
    """n 个短事务，每个写 1 个 key 后提交；每事务推进 1ms。"""
    for i in range(n):
        t = mgr.begin()
        mgr.write(t, f"{key_prefix}{i % 10}", f"v{i}")
        mgr.commit(t)
        clock.advance(dt)


class TestNoTransaction(unittest.TestCase):
    """情形 1：无事务。水位 = commit_seq，垃圾全部可回收。"""

    def test_watermark_equals_commit_seq_when_idle(self):
        clock, store, mgr, gc = make_env()
        self.assertEqual(mgr.watermark(), 0)
        run_short_txns(mgr, clock, 50)
        self.assertEqual(mgr.watermark(), mgr.commit_seq)
        before = store.total_versions()
        stats = gc.collect()
        self.assertEqual(stats.watermark, mgr.commit_seq)
        self.assertGreater(stats.versions_removed, 0)
        # 每个 key 只保留水位处可见的 1 个版本
        self.assertEqual(store.total_versions(), 10)
        self.assertLess(store.total_versions(), before)


class TestShortTransactions(unittest.TestCase):
    """情形 2：短事务。水位随事务结束持续推进，不积压。"""

    def test_watermark_advances_and_gc_reclaims(self):
        clock, store, mgr, gc = make_env()
        run_short_txns(mgr, clock, 200)
        self.assertEqual(mgr.identify_long_txns(), [])
        stats = gc.collect()
        self.assertEqual(stats.watermark, mgr.commit_seq)
        self.assertEqual(store.total_versions(), 10)
        # 再跑一轮，链长稳定 -> 水位没有卡住
        run_short_txns(mgr, clock, 200)
        gc.collect()
        self.assertEqual(store.total_versions(), 10)


class TestSingleOverlongTransaction(unittest.TestCase):
    """情形 3：单个超长事务。识别 -> 处置 -> 水位恢复推进。"""

    def test_readonly_long_txn_is_identified_then_degraded(self):
        clock, store, mgr, gc = make_env()
        run_short_txns(mgr, clock, 10)
        long_txn = mgr.begin()              # 只读长事务
        pinned_wm = mgr.watermark()
        run_short_txns(mgr, clock, 100)     # 背景短事务制造垃圾
        clock.advance(LONG_THRESHOLD + 1)   # 31s：超过识别阈值
        found = mgr.identify_long_txns()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["txn_id"], long_txn)
        self.assertTrue(found[0]["read_only"])
        self.assertFalse(found[0]["over_max"])
        self.assertEqual(mgr.enforce(), [])  # 未到处置阈值，不动作
        # 水位被长事务钉住
        self.assertEqual(mgr.watermark(), pinned_wm)
        clock.advance(MAX_DURATION)          # 151s：超过处置阈值
        events = mgr.enforce()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "degrade")
        self.assertEqual(events[0].txn_id, long_txn)
        # 处置后水位推进到 commit_seq
        self.assertEqual(mgr.watermark(), mgr.commit_seq)
        self.assertGreater(events[0].watermark_after,
                           events[0].watermark_before)
        # 降级后读到最新已提交数据，而不是旧快照
        self.assertEqual(mgr.read(long_txn, "k0"),
                         store.visible_at("k0", mgr.commit_seq).value)
        # 水位推进后 GC 可以回收积压
        stats = gc.collect()
        self.assertGreater(stats.versions_removed, 0)
        self.assertEqual(store.total_versions(), 10)

    def test_writer_long_txn_is_force_aborted(self):
        clock, store, mgr, gc = make_env()
        run_short_txns(mgr, clock, 10)
        long_txn = mgr.begin()
        mgr.write(long_txn, "k0", "dirty")   # 有写 -> 只能强制中断
        clock.advance(MAX_DURATION + 1)
        events = mgr.enforce()
        self.assertEqual([e.action for e in events], ["force_abort"])
        # 缓冲写被丢弃，从未进入版本链
        self.assertNotEqual(store.visible_at("k0", mgr.commit_seq).value,
                            "dirty")
        with self.assertRaises(KeyError):
            mgr.commit(long_txn)             # 已终止，不能再提交
        self.assertEqual(mgr.watermark(), mgr.commit_seq)

    def test_long_txn_blocks_gc_until_disposed(self):
        clock, store, mgr, gc = make_env()
        t0 = mgr.begin()
        run_short_txns(mgr, clock, 500)
        stats = gc.collect()                 # 水位被钉住
        self.assertEqual(stats.versions_removed, 0)
        clock.advance(MAX_DURATION + 1)
        mgr.enforce()
        stats = gc.collect()
        self.assertGreater(stats.versions_removed, 0)


class TestCleanupNeverCrossesWatermark(unittest.TestCase):
    """不变式：任何被回收版本的 commit_seq 都 < 水位，且水位处版本保留。"""

    def test_gc_invariant_with_pinned_watermark(self):
        clock, store, mgr, gc = make_env()
        run_short_txns(mgr, clock, 5)
        long_txn = mgr.begin()               # 钉住水位
        wm = mgr.watermark()
        run_short_txns(mgr, clock, 300)
        gc.collect()
        for key in store.keys():
            chain = store.chain(key)
            visible = store.visible_at(key, wm)
            below = [v for v in chain if v.commit_seq <= wm]
            if below:
                # 水位之下只剩最老活跃快照可见的那一个版本
                self.assertIsNotNone(visible)
                self.assertEqual(below, [visible])


class TestCrashRecovery(unittest.TestCase):
    """情形 4：清理中事务崩溃 + 崩溃后重建对拍。"""

    def test_crash_mid_cleanup_then_recover(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = os.path.join(d, "state.jsonl")
            clock, store, mgr, gc = make_env(log_path)
            run_short_txns(mgr, clock, 20)
            victim = mgr.begin()             # 清理期间客户端崩溃的事务
            mgr.write(victim, "k1", "uncommitted")
            run_short_txns(mgr, clock, 20)
            wm_before_crash = mgr.watermark()

            # 清理到一半注入崩溃
            calls = {"n": 0}
            def boom(key, removed):
                calls["n"] += 1
                if calls["n"] == 3:
                    raise CrashInjected()
            with self.assertRaises(CrashInjected):
                gc.collect(on_key=boom)
            # 崩溃瞬间：水位之下的回收已生效，但水位处版本仍在
            for key in store.keys():
                self.assertIsNotNone(store.visible_at(key, wm_before_crash))
            mgr.log.close()

            # 进程重启：从日志重建
            clock2 = ManualClock(start=clock.now())
            store2 = VersionStore()
            mgr2 = TransactionManager.recover(store2, clock2, log_path,
                                              long_threshold=LONG_THRESHOLD,
                                              max_duration=MAX_DURATION)
            # 未决事务被识别为孤儿并强制中止
            self.assertEqual(mgr2.recovered_orphans, [victim])
            self.assertEqual(mgr2.active_txns(), {})
            # 重建对拍：commit_seq / 水位 / 版本总数与崩溃前一致
            self.assertEqual(mgr2.commit_seq, mgr.commit_seq)
            self.assertEqual(mgr2.watermark(), mgr.commit_seq)
            self.assertEqual(store2.total_versions(),
                             40)               # 40 次提交各 1 版本
            for key in store2.keys():
                rebuilt = [(v.commit_seq, v.value) for v in store2.chain(key)]
                survived = [(v.commit_seq, v.value) for v in store.chain(key)]
                # 崩溃前未被 GC 回收的版本，恢复后必须原样存在
                for item in survived:
                    self.assertIn(item, rebuilt)
                # 恢复不会凭空造版本：全部来自已提交历史
                self.assertTrue(all(seq <= mgr.commit_seq
                                    for seq, _ in rebuilt))
            # 恢复后水位不再被孤儿钉住，GC 正常回收
            gc2 = GarbageCollector(store2, mgr2)
            stats = gc2.collect()
            self.assertEqual(stats.watermark, mgr2.commit_seq)
            self.assertEqual(store2.total_versions(), 10)
            mgr2.log.close()

    def test_recovery_rebuild_matches_snapshot(self):
        """对拍：崩溃前抓快照，恢复后逐项比对。"""
        with tempfile.TemporaryDirectory() as d:
            log_path = os.path.join(d, "state.jsonl")
            clock, store, mgr, gc = make_env(log_path)
            run_short_txns(mgr, clock, 30)
            t1 = mgr.begin()                 # 活跃长事务（有 terminal 前崩溃）
            run_short_txns(mgr, clock, 30)
            t2 = mgr.begin()
            mgr.abort(t2)                    # 显式中止的不算孤儿
            run_short_txns(mgr, clock, 10)
            snapshot = {
                "commit_seq": mgr.commit_seq,
                "watermark": mgr.watermark(),
                "versions": sorted(
                    (k, v.commit_seq, v.value)
                    for k in store.keys() for v in store.chain(k)),
            }
            mgr.log.close()                  # 模拟崩溃（t1 无终态）

            store2 = VersionStore()
            mgr2 = TransactionManager.recover(
                store2, ManualClock(start=clock.now()), log_path,
                long_threshold=LONG_THRESHOLD, max_duration=MAX_DURATION)
            rebuilt = {
                "commit_seq": mgr2.commit_seq,
                "watermark": mgr2.watermark(),
                "versions": sorted(
                    (k, v.commit_seq, v.value)
                    for k in store2.keys() for v in store2.chain(k)),
            }
            self.assertEqual(mgr2.recovered_orphans, [t1])
            # 孤儿被清理后水位 = commit_seq；其余逐项相等
            self.assertEqual(rebuilt["commit_seq"], snapshot["commit_seq"])
            self.assertEqual(rebuilt["versions"], snapshot["versions"])
            self.assertEqual(rebuilt["watermark"], snapshot["commit_seq"])
            mgr2.log.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

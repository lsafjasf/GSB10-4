"""Self-tests for long-transaction resource control.

Run:  python3 -m unittest discover -s tests -v
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from longtxn import (
    GCCoordinator,
    ManualClock,
    MVCCStore,
    TxnManager,
    TxnState,
)


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock(0.0)
        self.store = MVCCStore()
        self.mgr = TxnManager(self.clock.now)

    def write(self, key, value, dt=1.0):
        ts = self.clock.advance(dt)
        self.store.put(key, value, ts)
        return ts


class TestNoTransactions(Base):
    """无事务：水位直接推到当前时间，旧版本全部回收（每键保留最新一条）。"""

    def test_gc_with_no_txns(self):
        for i in range(10):
            self.write("k", f"v{i}")
        self.assertEqual(self.store.version_count(), 10)
        self.assertEqual(self.mgr.water_level(), self.clock.now())

        coord = GCCoordinator(self.store, self.mgr)
        stats = coord.run_gc()
        self.assertEqual(stats.removed_versions, 9)
        self.assertEqual(self.store.version_count(), 1)
        self.assertEqual(self.store.read("k", self.clock.now()), "v9")
        # 幂等：再跑一次不再删
        self.assertEqual(coord.run_gc().removed_versions, 0)


class TestShortTransactions(Base):
    """短事务：提交后不再阻塞水位，水位随其结束而推进。"""

    def test_short_txn_does_not_block(self):
        t1 = self.mgr.begin()                      # start_ts = 0
        self.write("a", 1)
        self.write("a", 2)
        self.assertEqual(self.mgr.water_level(), 0.0)   # 被 t1 压住
        self.mgr.commit(t1)
        self.write("a", 3)
        self.assertEqual(self.mgr.water_level(), self.clock.now())
        stats = GCCoordinator(self.store, self.mgr).run_gc()
        self.assertEqual(stats.removed_versions, 2)
        self.assertEqual(self.store.read("a", self.clock.now()), 3)


class TestLongTransaction(Base):
    """单个超长事务：识别 -> 超时限强制中断 -> 水位推进 -> 内存回收。"""

    def test_identify_then_force_abort(self):
        long_txn = self.mgr.begin()                # start_ts = 0
        # 阈值内持续写入，产生 30 个旧版本（每次 0.5s，共 15s）
        for i in range(30):
            self.write("k", i, dt=0.5)

        # 阈值内：不算长事务
        r0 = self.mgr.scan()
        self.assertEqual(r0.identified, [])
        self.assertEqual(self.mgr.water_level(), 0.0)

        # 到达 30s：被识别为长事务，但未被处置
        self.clock.set(30.0)
        r1 = self.mgr.scan()
        self.assertEqual([t.txn_id for t in r1.identified], [long_txn.txn_id])
        self.assertEqual(long_txn.state, TxnState.ACTIVE)
        self.assertIsNotNone(long_txn.identified_long_at)

        # 识别是一次性的
        self.assertEqual(self.mgr.scan().identified, [])

        # 水位仍被压住：GC 不得越过 start_ts=0
        coord = GCCoordinator(self.store, self.mgr)
        stats = coord.run_gc()
        self.assertEqual(stats.water_level, 0.0)
        self.assertEqual(stats.removed_versions, 0)
        self.assertEqual(self.store.version_count(), 30)

        # 到达 120s：超过时限，强制中断
        self.clock.set(120.0)
        r2 = self.mgr.scan()
        self.assertEqual([t.txn_id for t in r2.killed], [long_txn.txn_id])
        self.assertEqual(long_txn.state, TxnState.FORCED_ABORT)
        self.assertEqual(long_txn.killed_at, 120.0)

        # 处置结果：水位推进到当前时间，旧版本被回收
        self.assertEqual(self.mgr.water_level(), 120.0)
        stats2 = coord.run_gc()
        self.assertEqual(stats2.removed_versions, 29)
        self.assertEqual(self.store.version_count(), 1)
        self.assertEqual(self.store.read("k", 120.0), 29)

    def test_water_level_never_crossed(self):
        """清理不得越过水位线：水位处的可见版本必须保留。"""
        blocker = self.mgr.begin()                 # start_ts = 0
        ts1 = self.write("x", "old")               # 1.0
        ts2 = self.write("x", "mid")               # 2.0
        ts3 = self.write("x", "new")               # 3.0
        coord = GCCoordinator(self.store, self.mgr)
        coord.run_gc()                             # water_level = 0
        # 水位 0 之下没有版本，什么都不许删
        self.assertEqual(self.store.version_count(), 3)

        self.mgr.commit(blocker)
        reader = self.mgr.begin()                  # start_ts = 3.0
        ts4 = self.write("x", "newer")             # 4.0
        coord.run_gc()                             # water_level = 3.0
        # 3.0 处可见的是 "new"，它必须保留；old/mid 被回收
        self.assertEqual(self.store.read("x", 3.0), "new")
        self.assertEqual(self.store.version_count(), 2)
        self.assertEqual(self.store.read("x", 4.0), "newer")
        self.mgr.commit(reader)
        coord.run_gc()
        self.assertEqual(self.store.version_count(), 1)
        self.assertEqual(self.store.read("x", self.clock.now()), "newer")


class TestCrashRecovery(Base):
    """崩溃后重建 + 对拍：恢复出的水位/活跃集/可见数据必须与崩溃前一致。"""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="longtxn-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _build_world(self):
        clock = ManualClock(0.0)
        store = MVCCStore()
        mgr = TxnManager(clock.now)
        long_txn = mgr.begin()                     # start 0，长事务
        for i in range(5):
            ts = clock.advance(1.0)
            store.put("alpha", f"a{i}", ts)
            store.put("beta", f"b{i}", ts)
        short = mgr.begin()                        # start 5.0
        mgr.commit(short)
        clock.advance(1.0)
        store.put("alpha", "a5", clock.now())
        coord = GCCoordinator(store, mgr, state_dir=self.dir)
        coord.run_gc()                             # 持久化 state.json + journal done
        return clock, store, mgr, long_txn, coord

    def test_recovery_rebuild_matches_precrash(self):
        clock, store, mgr, long_txn, coord = self._build_world()

        # 崩溃前对拍数据（A 面）
        pre = {
            "water_level": mgr.water_level(),
            "active_ids": sorted(t.txn_id for t in mgr.active_txns()),
            "visible_at_wl": store.visible_dump(mgr.water_level()),
            "visible_now": store.visible_dump(clock.now()),
            "versions": store.version_count(),
        }

        # 模拟崩溃：丢掉全部内存对象，只留磁盘
        del clock, store, mgr, coord

        # 恢复：从 state.json 重建 TxnManager，水位由最老活跃事务重建
        state = GCCoordinator.read_state(self.dir)
        self.assertIsNotNone(state)
        clock2 = ManualClock(state["completed_water_level"])
        mgr2 = TxnManager.restore(state, clock2.now)
        # 重建存储：真实系统中数据页是持久化的，这里回放同一棵版本链
        store2 = MVCCStore()
        c = ManualClock(0.0)
        for i in range(5):
            ts = c.advance(1.0)
            store2.put("alpha", f"a{i}", ts)
            store2.put("beta", f"b{i}", ts)
        c.advance(1.0)
        store2.put("alpha", "a5", c.now())
        coord2 = GCCoordinator(store2, mgr2, state_dir=self.dir)
        coord2.recover()

        # 崩溃后对拍数据（B 面）
        post = {
            "water_level": mgr2.water_level(),
            "active_ids": sorted(t.txn_id for t in mgr2.active_txns()),
            "visible_at_wl": store2.visible_dump(mgr2.water_level()),
            "visible_now": store2.visible_dump(c.now()),
            "versions": store2.version_count(),
        }

        # 对拍：逐项相等
        self.assertEqual(pre["water_level"], post["water_level"])
        self.assertEqual(pre["active_ids"], post["active_ids"])
        self.assertEqual(pre["visible_at_wl"], post["visible_at_wl"])
        self.assertEqual(pre["visible_now"], post["visible_now"])
        self.assertEqual(pre["versions"], post["versions"])

        print("\n[recovery A/B] pre :", pre)
        print("[recovery A/B] post:", post)

    def test_crash_during_cleanup_replays(self):
        """清理中崩溃：journal=started 未完成，恢复后重放，结果与无崩溃对拍一致。"""
        clock, store, mgr, long_txn, coord = self._build_world()
        # 让长事务被强制中断，水位大幅推进，然后 GC 在 started 后"崩溃"
        clock.set(200.0)
        mgr.scan()                                 # force-abort long_txn
        wl = mgr.water_level()
        # 处置决定先落盘（run_gc 的第 2 步），随后才写清理 journal
        coord._persist_state(wl, "gc_pending")
        coord._write_journal("started", wl)        # 模拟：写完 journal 就宕机
        # --- crash ---
        state = GCCoordinator.read_state(self.dir)
        # 恢复时钟：优先用已完成水位，否则用待完成 GC 的目标水位
        clock2 = ManualClock(state.get("completed_water_level", state["target_water_level"]))
        mgr2 = TxnManager.restore(state, clock2.now)
        # 崩溃前未完成的 GC 必须被重放
        coord2 = GCCoordinator(store, mgr2, state_dir=self.dir)
        replayed = coord2.recover()
        self.assertIsNotNone(replayed)
        self.assertEqual(replayed.water_level, wl)
        # 重放后：长事务不再阻塞，旧版本被清掉
        self.assertEqual(store.version_count(), 2)  # alpha,beta 各留最新 1 条
        self.assertEqual(store.read("alpha", clock.now()), "a5")
        self.assertEqual(store.read("beta", clock.now()), "b4")
        # 再跑一次正常 GC 不再删（幂等）
        self.assertEqual(coord2.run_gc().removed_versions, 0)

    def test_txn_crashes_while_cleanup_runs(self):
        """清理进行中某事务崩溃（连接断开）：中止后水位立即释放，结果与正常中止对拍。"""
        clock = ManualClock(0.0)
        store = MVCCStore()
        mgr = TxnManager(clock.now)
        victim = mgr.begin()
        for i in range(10):
            ts = clock.advance(1.0)
            store.put("k", i, ts)

        # A 面：清理中事务崩溃 -> abort -> GC
        coord_a = GCCoordinator(store, mgr)
        coord_a._write_journal("started", mgr.water_level())  # GC 进行中
        mgr.abort(victim)                                     # 事务崩溃
        stats_a = coord_a.run_gc()

        # B 面：同样场景但事务正常 abort 后再 GC（无并发崩溃）
        clock_b = ManualClock(0.0)
        store_b = MVCCStore()
        mgr_b = TxnManager(clock_b.now)
        victim_b = mgr_b.begin()
        for i in range(10):
            ts = clock_b.advance(1.0)
            store_b.put("k", i, ts)
        mgr_b.abort(victim_b)
        stats_b = GCCoordinator(store_b, mgr_b).run_gc()

        # 对拍：两条路径最终状态一致
        self.assertEqual(stats_a.water_level, stats_b.water_level)
        self.assertEqual(stats_a.removed_versions, stats_b.removed_versions)
        self.assertEqual(store.version_count(), store_b.version_count())
        self.assertEqual(store.visible_dump(clock.now()), store_b.visible_dump(clock_b.now()))
        print(f"\n[txn-crash A/B] A={stats_a} B={stats_b}")


if __name__ == "__main__":
    unittest.main()

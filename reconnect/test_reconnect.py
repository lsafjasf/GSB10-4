# -*- coding: utf-8 -*-
"""断线重连状态重建自测（python3 -m unittest -v test_reconnect）。

覆盖：正常续传 / 被迫全量（无凭据、版本不一致、摘要不符、位点超前）/
位点过期 / 重连期间再次断开（含交错重连）/ 反复重连状态轨迹 /
续传与全量对拍。
"""
import os
import random
import unittest

from reconnect_lib import Client, ResumeToken, Server

OUT_DIR = os.path.dirname(os.path.abspath(__file__))


def make_events(server, n, rng, key_space=50):
    """在 server 上发布 n 条随机 set/del 事件。"""
    for _ in range(n):
        key = "k%03d" % rng.randrange(key_space)
        if rng.random() < 0.85:
            server.publish("set", key, rng.randrange(1 << 30))
        else:
            server.publish("del", key)


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(20261005)

    # ---- 摘要本身正确 ------------------------------------------------------
    def test_digest_incremental_matches_full_recompute(self):
        server = Server(retention=10 ** 9)
        make_events(server, 5000, self.rng)
        self.assertEqual(server.state.digest, server.state.recompute_digest())

    # ---- 正常续传 ----------------------------------------------------------
    def test_normal_resume_no_gap_no_duplicate(self):
        server = Server(retention=10 ** 6)
        make_events(server, 100, self.rng)
        c = Client()
        self.assertEqual(c.sync(server), "full")      # 首次无凭据 -> 全量
        self.assertEqual(c.offset, 100)

        make_events(server, 60, self.rng)
        self.assertEqual(c.sync(server), "resume")   # 第二次 -> 续传
        self.assertEqual(c.offset, server.seq)
        self.assertEqual(c.state, server.state.data)
        self.assertEqual(c.store.digest, server.state.digest)
        self.assertEqual(c.stats["resume"], 1)
        self.assertEqual(c.stats["full"], 1)
        # 摘要与"全量独立重算"一致 => 没丢更新、没重复应用
        self.assertEqual(c.store.digest, c.store.recompute_digest())

    # ---- 被迫全量：各种不可用/过期依据 ------------------------------------
    def test_full_when_no_token(self):
        server = Server()
        make_events(server, 10, self.rng)
        self.assertEqual(server.decide(None).kind, "full")

    def test_full_when_version_mismatch(self):
        server = Server(retention=10 ** 6)
        make_events(server, 20, self.rng)
        c = Client()
        c.sync(server)
        make_events(server, 5, self.rng)
        server.bump_version()                          # 服务端换世代
        d = server.decide(c.token())
        self.assertEqual(d.kind, "full")
        self.assertIn("版本", d.reason)
        self.assertEqual(c.sync(server), "full")
        self.assertEqual(c.state, server.state.data)

    def test_full_when_digest_mismatch(self):
        server = Server(retention=10 ** 6)
        make_events(server, 20, self.rng)
        c = Client()
        c.sync(server)
        make_events(server, 5, self.rng)
        # 模拟本地状态被污染：摘要与真实状态不符
        c.store.digest ^= 0xDEADBEEF
        d = server.decide(c.token())
        self.assertEqual(d.kind, "full")
        self.assertIn("摘要", d.reason)
        self.assertEqual(c.sync(server), "full")
        self.assertEqual(c.state, server.state.data)

    def test_full_when_offset_ahead(self):
        server = Server()
        make_events(server, 10, self.rng)
        bad = ResumeToken(999, server.version, 0)
        d = server.decide(bad)
        self.assertEqual(d.kind, "full")
        self.assertIn("超前", d.reason)

    # ---- 位点过期 ----------------------------------------------------------
    def test_full_when_offset_expired(self):
        server = Server(retention=10)
        make_events(server, 10, self.rng)
        c = Client()
        c.sync(server)
        make_events(server, 100, self.rng)             # 回放窗口滑走
        self.assertLess(c.offset, server.base_seq)
        d = server.decide(c.token())
        self.assertEqual(d.kind, "full")
        self.assertIn("过期", d.reason)
        self.assertEqual(c.sync(server), "full")
        self.assertEqual(c.state, server.state.data)
        self.assertEqual(c.store.digest, server.state.digest)

    # ---- 重连期间再次断开（连续抖动、交错 drop_after）--------------------
    def test_drop_during_resume_and_interleaved_drops(self):
        server = Server(retention=10 ** 6)
        make_events(server, 50, self.rng)
        c = Client()
        c.sync(server)
        make_events(server, 300, self.rng)

        offsets = []
        pattern = [3, 1, 7, None, 2, None, 5, 1, None]  # None=当次不断
        for drop_after in pattern:
            r = c.sync_once(server, drop_after=drop_after)
            offsets.append((drop_after, c.offset))
            if c.offset == server.seq:
                self.assertIsNotNone(r)
                break
        else:
            self.fail("交错断连序列下未能追平")

        self.assertEqual(c.offset, server.seq)
        self.assertEqual(c.state, server.state.data)
        self.assertEqual(c.store.digest, c.store.recompute_digest())
        # 每次断开后位点只增不减；同一事件绝不会被应用两次（序号硬校验保证）
        applied_offsets = [o for _, o in offsets]
        self.assertEqual(applied_offsets, sorted(applied_offsets))
        self.assertGreater(c.stats["drops"], 0)

    def test_drop_then_event_trimmed_forces_renegotiation(self):
        """传输中所需事件被清理：当次连接失败，下次重连安全退化为全量。"""
        server = Server(retention=20)
        make_events(server, 20, self.rng)
        c = Client()
        c.sync(server)
        make_events(server, 19, self.rng)             # 还差 19 条
        # 每次只给 1 条；与此同时持续推进服务端并滑走窗口
        r = c.sync_once(server, drop_after=1)
        self.assertIsNone(r)
        make_events(server, 200, self.rng)
        self.assertLess(c.offset, server.base_seq)
        self.assertEqual(c.sync(server), "full")
        self.assertEqual(c.state, server.state.data)

    # ---- 反复重连：状态轨迹 ------------------------------------------------
    def test_repeated_reconnect_trajectory(self):
        server = Server(retention=10 ** 6)
        c = Client()
        rows = []
        schedule = [(0, None), (40, 6), (40, None), (40, 13), (40, None),
                    (40, 4), (40, None)]
        # (本轮新增事件数, drop_after)
        for round_no, (new_events, drop_after) in enumerate(schedule, 1):
            make_events(server, new_events, self.rng)
            attempts = 0
            path = None
            while True:
                attempts += 1
                r = c.sync_once(server, drop_after=drop_after)
                if r is not None:
                    path = r
                    break
                self.assertLessEqual(attempts, new_events + 1)
            rows.append((round_no, path, attempts, c.offset,
                         "%016x" % c.store.digest, len(c.state)))
            self.assertEqual(c.state, server.state.data)

        header = ("round", "path", "attempts", "offset",
                  "digest@offset", "|state|")
        lines = ["\t".join(header)]
        for row in rows:
            lines.append("\t".join(map(str, row)))
        table = "\n".join(lines)
        with open(os.path.join(OUT_DIR, "trajectory.txt"), "w") as f:
            f.write(table + "\n")
        print("\n[state trajectory]\n" + table)
        self.assertEqual(rows[0][1], "full")
        self.assertGreater(sum(1 for r in rows if r[1] == "resume"), 0)

    # ---- 对拍：续传结果 vs 全量重放结果 vs 服务端权威 ---------------------
    def test_resume_vs_full_differential(self):
        server = Server(retention=10 ** 6)
        cr = Client("resume-client")   # 走真实续传判定
        cf = Client("full-client")     # 每次连接强制全量
        make_events(server, 500, self.rng)
        self.assertEqual(cr.sync(server), "full")
        cf.sync(server)

        rounds = 60
        per_round = 300
        sample_rows = []
        for round_no in range(1, rounds + 1):
            make_events(server, per_round, self.rng)
            # 续传客户端：随机抖动，可能多次断连
            while True:
                r = cr.sync_once(
                    server,
                    drop_after=self.rng.choice([None, 1, 5, 17, 99]),
                )
                if r is not None:
                    break
            # 全量客户端：丢弃凭据，每次全量
            cf.version = 0
            cf.sync(server)
            self.assertEqual(cr.offset, server.seq)
            self.assertEqual(cf.offset, server.seq)
            self.assertEqual(cr.store.digest, cf.store.digest)
            self.assertEqual(cr.state, cf.state)
            self.assertEqual(cr.state, server.state.data)
            if round_no <= 8 or round_no % 10 == 0:
                sample_rows.append((round_no, server.seq,
                                    "%016x" % cr.store.digest,
                                    "%016x" % cf.store.digest,
                                    cr.state == cf.state))

        header = ("round", "server_seq", "resume_digest",
                  "full_digest", "state_equal")
        lines = [",".join(header)]
        for row in sample_rows:
            lines.append(",".join(map(str, row)))
        table = "\n".join(lines)
        with open(os.path.join(OUT_DIR, "differential.csv"), "w") as f:
            f.write(table + "\n")
        print("\n[differential: resume vs full replay]\n" + table)
        # 每次重连尝试恰好完成一次握手；drops 是握手成功后又被断开的子集
        self.assertEqual(cr.stats["resume"] + cr.stats["full"],
                         cr.stats["attempts"])
        self.assertLessEqual(cr.stats["drops"], cr.stats["attempts"])
        self.assertGreater(cr.stats["resume"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

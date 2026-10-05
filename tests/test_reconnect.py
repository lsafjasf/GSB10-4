"""断线重连状态重建的自测。运行：python3 -m unittest discover -s tests -v"""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reconnect import (
    Checkpoint,
    Client,
    FullReplay,
    ReconnectDropped,
    Resume,
    Server,
    digest_of,
)


def fresh_full_replay_view(server: Server) -> dict:
    """对拍基准：一个全新客户端走全量重放得到的状态。"""
    c = Client()
    d = c.sync(server)
    assert isinstance(d, FullReplay)
    return c.state


class TestNormalResume(unittest.TestCase):
    """情形 1：正常续传 —— 不重不漏，且与全量重放对拍一致。"""

    def test_resume_matches_full_replay(self):
        server = Server()
        for i in range(40):
            server.publish("inc", f"k{i % 7}", 1)
        client = Client()
        client.sync(server)                      # 首次：全量
        for i in range(60):
            server.publish("inc", f"k{i % 7}", 1)
            server.publish("set", f"s{i % 5}", i)
        d = client.sync(server)                  # 断线重连：应走续传
        self.assertIsInstance(d, Resume)
        self.assertEqual(len(d.events), 120)
        seqs = [e.seq for e in d.events]
        self.assertEqual(seqs, list(range(41, 161)))   # 不重不漏
        self.assertEqual(client.state, server.state)
        self.assertEqual(client.state, fresh_full_replay_view(server))  # 对拍

    def test_resume_with_no_gap_is_noop(self):
        server = Server()
        for i in range(10):
            server.publish("inc", "a", 1)
        client = Client()
        client.sync(server)
        d = client.sync(server)                  # 无新事件，续传 0 条
        self.assertIsInstance(d, Resume)
        self.assertEqual(len(d.events), 0)
        self.assertEqual(client.state, server.state)


class TestForcedFullReplay(unittest.TestCase):
    """情形 2：被迫全量 —— 版本号不一致。"""

    def test_version_mismatch_forces_full(self):
        server = Server()
        for i in range(30):
            server.publish("inc", "a", 1)
        client = Client()
        client.sync(server)
        server.rotate_version()                  # 状态基线整体更换
        for i in range(5):
            server.publish("set", "b", i)
        d = client.sync(server)
        self.assertIsInstance(d, FullReplay)
        self.assertEqual(d.reason, "version-mismatch")
        self.assertEqual(client.state, server.state)
        self.assertEqual(client.checkpoint.version, server.version)

    def test_client_ahead_of_server_forces_full(self):
        server = Server()
        for i in range(10):
            server.publish("inc", "a", 1)
        client = Client()
        client.sync(server)
        # 伪造一个比服务端还新的位点（如连错了集群）：不许猜，必须全量
        client.checkpoint = Checkpoint(server.version, 999, client.checkpoint.digest)
        d = client.sync(server)
        self.assertIsInstance(d, FullReplay)
        self.assertEqual(d.reason, "seq-ahead-of-server")
        self.assertEqual(client.state, server.state)

    def test_digest_mismatch_forces_full(self):
        server = Server()
        for i in range(10):
            server.publish("inc", "a", 1)
        client = Client()
        client.sync(server)
        client.state["a"] = -12345               # 本地状态被改坏
        client.checkpoint = Checkpoint(
            client.checkpoint.version,
            client.checkpoint.seq,
            digest_of(client.state),
        )
        d = client.sync(server)
        self.assertIsInstance(d, FullReplay)
        self.assertEqual(d.reason, "digest-mismatch")
        self.assertEqual(client.state, server.state)

    def test_digest_unavailable_forces_full(self):
        server = Server()
        for i in range(20):
            server.publish("inc", "a", 1)
        client = Client()
        client.sync(server)
        server.lose_digests()                    # 摘要不可考：安全退化
        d = client.sync(server)
        self.assertIsInstance(d, FullReplay)
        self.assertEqual(d.reason, "digest-unavailable")
        self.assertEqual(client.state, server.state)


class TestSeqExpired(unittest.TestCase):
    """情形 3：位点过期 —— 日志截断后旧位点不可续。"""

    def test_expired_seq_forces_full(self):
        server = Server()
        for i in range(50):
            server.publish("inc", f"k{i % 3}", 1)
        client = Client()
        client.sync(server)                      # 客户端位点停在 50
        for i in range(50):
            server.publish("inc", f"k{i % 3}", 1)
        server.compact(keep_last=10)             # base_seq = 90 > 50
        d = client.sync(server)
        self.assertIsInstance(d, FullReplay)
        self.assertEqual(d.reason, "seq-expired")
        self.assertEqual(client.state, server.state)

    def test_seq_at_boundary_still_resumes(self):
        server = Server()
        for i in range(50):
            server.publish("inc", "a", 1)
        client = Client()
        client.sync(server)
        for i in range(10):
            server.publish("inc", "a", 1)
        server.compact(keep_last=10)             # base_seq = 50，恰好等于客户端位点
        d = client.sync(server)
        self.assertIsInstance(d, Resume)         # 边界上位点仍有效
        self.assertEqual(client.state, server.state)


class TestDisconnectDuringReconnect(unittest.TestCase):
    """情形 4：重连期间再次断开 —— 部分批次不提交，重试后一致。"""

    def test_partial_batch_not_committed(self):
        server = Server()
        for i in range(20):
            server.publish("inc", "a", 1)
        client = Client()
        client.sync(server)
        cp_before = client.checkpoint
        state_before = dict(client.state)
        for i in range(30):
            server.publish("inc", "a", 1)
        with self.assertRaises(ReconnectDropped) as ctx:
            client.sync(server, deliver=7)       # 30 条只到了 7 条就断
        self.assertEqual((ctx.exception.delivered, ctx.exception.total), (7, 30))
        self.assertEqual(client.checkpoint, cp_before)   # 位点没动
        self.assertEqual(client.state, state_before)     # 状态没脏
        d = client.sync(server)                          # 再次重连：完整续传
        self.assertIsInstance(d, Resume)
        self.assertEqual(len(d.events), 30)
        self.assertEqual(client.state, server.state)

    def test_repeated_drops_eventually_converge(self):
        server = Server()
        for i in range(100):
            server.publish("inc", "a", 1)
        client = Client()
        client.sync(server)
        for i in range(100):
            server.publish("inc", "a", 1)
        for delivered in (0, 1, 50, 99):         # 连续多次重连都中途断
            with self.assertRaises(ReconnectDropped):
                client.sync(server, deliver=delivered)
        client.sync(server)
        self.assertEqual(client.state, server.state)
        self.assertEqual(client.state, fresh_full_replay_view(server))

    def test_snapshot_delivery_drop_is_atomic(self):
        server = Server()
        for i in range(10):
            server.publish("inc", "a", 1)
        client = Client()
        client.sync(server)
        server.rotate_version()                  # 触发全量
        with self.assertRaises(ReconnectDropped):
            client.sync(server, deliver=0)       # 快照没传完就断
        self.assertEqual(client.checkpoint.version, 0)   # 旧版本位点保留
        d = client.sync(server)
        self.assertIsInstance(d, FullReplay)
        self.assertEqual(client.state, server.state)


class TestInterleavedReconnects(unittest.TestCase):
    """反复重连 + 多客户端交错重连不破坏状态。"""

    def test_interleaved_clients_converge(self):
        server = Server()
        clients = [Client() for _ in range(3)]
        rng = random.Random(20261005)
        for step in range(300):
            server.publish(rng.choice(["inc", "set", "del"]),
                           f"k{rng.randrange(6)}", rng.randrange(10))
            c = clients[rng.randrange(3)]
            try:
                if rng.random() < 0.3:           # 30% 概率传输中断
                    c.sync(server, deliver=rng.randrange(0, 3))
                else:
                    c.sync(server)
            except ReconnectDropped:
                pass
        for c in clients:
            c.sync(server)
            self.assertEqual(c.state, server.state)
            self.assertEqual(c.state, fresh_full_replay_view(server))


class TestResumeVsFullFuzz(unittest.TestCase):
    """对拍：随机事件流 + 随机断连/截断/版本更替，续传结果必须等于全量重放。"""

    def test_fuzz_resume_equals_full(self):
        rng = random.Random(42)
        comparisons = 0
        for trial in range(200):
            server = Server()
            client = Client()
            for _ in range(rng.randrange(1, 60)):
                server.publish(rng.choice(["inc", "set", "del"]),
                               f"k{rng.randrange(4)}", rng.randrange(5))
            client.sync(server)
            for _ in range(rng.randrange(0, 60)):
                server.publish(rng.choice(["inc", "set", "del"]),
                               f"k{rng.randrange(4)}", rng.randrange(5))
            r = rng.random()
            if r < 0.15:
                server.compact(keep_last=rng.randrange(0, 5))
            elif r < 0.25:
                server.rotate_version()
            elif r < 0.35:
                server.lose_digests()
            deliver = rng.randrange(0, 200) if rng.random() < 0.3 else None
            try:
                client.sync(server, deliver=deliver)
            except ReconnectDropped:
                client.sync(server)
            self.assertEqual(client.state, fresh_full_replay_view(server),
                             f"trial {trial}: 续传与全量重放结果不一致")
            comparisons += 1
        self.assertEqual(comparisons, 200)


if __name__ == "__main__":
    unittest.main()


class TestIncrementalDigest(unittest.TestCase):
    """增量摘要必须始终等于全量重算，否则续传校验会误判。"""

    def test_digest_apply_matches_digest_of(self):
        from reconnect import apply_op, digest_apply, Op
        rng = random.Random(7)
        state = {}
        digest = digest_of(state)
        for i in range(2000):
            op = Op(i + 1, rng.choice(["inc", "set", "del"]),
                    f"k{rng.randrange(8)}", rng.randrange(5))
            k, old, new = apply_op(state, op)
            digest = digest_apply(digest, k, old, new)
            self.assertEqual(digest, digest_of(state))

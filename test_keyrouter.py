"""键路由一致性自测。

运行： python3 -m unittest -v test_keyrouter
"""

import unittest

from keyrouter import (
    MergingConsumer,
    PartitionedLog,
    Producer,
    Router,
)


class RoutingRulesTest(unittest.TestCase):
    def test_stable_when_partition_count_unchanged(self):
        r1, r2 = Router(16), Router(16)
        keys = ["user:%d" % i for i in range(2000)]
        # 跨实例、重复调用、范围合法，三者一致
        for key in keys:
            p = r1.partition_for(key)
            self.assertEqual(p, r1.partition_for(key))
            self.assertEqual(p, r2.partition_for(key))
            self.assertGreaterEqual(p, 0)
            self.assertLess(p, 16)

    def test_single_partition(self):
        router = Router(1)
        for key in ["a", "", "x", None, b"", 42]:
            self.assertEqual(router.partition_for(key), 0)

    def test_different_keys_spread_over_partitions(self):
        router = Router(16)
        used = {router.partition_for("k-%d" % i) for i in range(2000)}
        self.assertEqual(used, set(range(16)))

    def test_doubling_moves_about_half_the_keys(self):
        # HRW 性质：N -> 2N 时每个键落进“新半区”的概率为 1/2，
        # 即平均迁移一半键，且不会出现全量重映射。
        old, new = Router(8), Router(16)
        keys = ["k-%d" % i for i in range(10000)]
        moved = sum(
            old.partition_for(k) != new.partition_for(k) for k in keys
        )
        ratio = moved / len(keys)
        self.assertAlmostEqual(ratio, 0.5, delta=0.05)
        self.assertGreater(moved, 0)
        self.assertLess(moved, len(keys))

    def test_invalid_partition_count(self):
        with self.assertRaises(ValueError):
            Router(0)

    def test_empty_keys_are_spread_deterministically(self):
        n = 8
        router = Router(n)
        assigned = [router.partition_for("") for _ in range(40)]
        # 不压在一个分区：轮转摊到全部 8 个分区
        self.assertEqual(sorted(set(assigned)), list(range(n)))

        # 确定性：相同调用序列，新实例给出完全一致的结果
        router2 = Router(n)
        assigned2 = [router2.partition_for("") for _ in range(40)]
        self.assertEqual(assigned, assigned2)

    def test_hot_key_stays_on_one_partition(self):
        # 热点键为保顺序固定在一个分区，绝不拆分
        router = Router(32)
        partitions = {router.partition_for("hot-key") for _ in range(100000)}
        self.assertEqual(len(partitions), 1)


class MigrationOrderTest(unittest.TestCase):
    def _run_migration(
        self, old_n, new_n, before, during, after, key_count
    ):
        old_log, new_log = PartitionedLog(old_n), PartitionedLog(new_n)
        old_r, new_r = Router(old_n), Router(new_n)
        producer = Producer(old_r, new_r, old_log, new_log)
        consumer = MergingConsumer()

        keys = ["key-%04d" % i for i in range(key_count)]

        # 阶段 1：迁移前，旧布局写入
        for i in range(before):
            producer.send(keys[i % key_count], payload="b%d" % i)
        self.assertEqual(consumer.poll(old_log), before)

        # 阶段 2：双写过渡期，迁移中持续写入，并在两个日志交错读取
        producer.mode = Producer.DUAL
        for i in range(during):
            producer.send(keys[i % key_count], payload="d%d" % i)
            if i % 7 == 0:  # 刻意交错轮询，制造乱序到达
                consumer.poll(old_log, new_log)
        consumer.poll(old_log, new_log)

        # 安全切换条件：旧日志已被全部消费（栅栏点）
        self.assertTrue(consumer.consumed_all(old_log))
        producer.mode = Producer.NEW

        # 阶段 3：切换后，新布局继续写入
        for i in range(after):
            producer.send(keys[i % key_count], payload="a%d" % i)
        consumer.poll(new_log)
        self.assertTrue(consumer.consumed_all(new_log))

        return producer, consumer, old_r, new_r

    def test_order_preserved_through_doubling(self):
        producer, consumer, old_r, new_r = self._run_migration(
            old_n=4, new_n=8, before=500, during=1000, after=500,
            key_count=50,
        )

        # 对拍：每个键交付负载必须与生产者发送记录逐字节一致
        report = consumer.order_report()
        self.assertEqual(len(report), len(producer.totals_by_key()))
        for key, count, first_seq, last_seq, ok in report:
            self.assertTrue(ok, f"键 {key} 出现乱序/缺口")
            self.assertEqual(first_seq, 0)
            self.assertEqual(last_seq, count - 1)
            self.assertEqual(
                consumer.delivered[key], producer.sent_payloads(key)
            )

        # 双写期消息只交付一次（去重生效）
        total_delivered = sum(
            len(v) for v in consumer.delivered.values()
        )
        self.assertEqual(total_delivered, 500 + 1000 + 500)

    def test_single_partition_migration(self):
        # 1 -> 4：极端扩容场景顺序同样不允许断
        producer, consumer, _, _ = self._run_migration(
            old_n=1, new_n=4, before=200, during=400, after=200,
            key_count=20,
        )
        for key, _, _, _, ok in consumer.order_report():
            self.assertTrue(ok, f"键 {key} 在 1->4 迁移后乱序")
            self.assertEqual(
                consumer.delivered[key], producer.sent_payloads(key)
            )

    def test_no_duplicates_under_repeated_polls(self):
        old_log, new_log = PartitionedLog(4), PartitionedLog(8)
        p = Producer(Router(4), Router(8), old_log, new_log)
        c = MergingConsumer()
        p.mode = Producer.DUAL
        for i in range(300):
            p.send("k", payload=i)
        for _ in range(5):
            c.poll(old_log, new_log)
        self.assertEqual(c.delivered["k"], list(range(300)))
        self.assertEqual(c.delivered_seqs["k"], list(range(300)))


if __name__ == "__main__":
    unittest.main(verbosity=2)

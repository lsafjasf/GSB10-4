import random
import unittest

from keyrouter import (
    Broker,
    Consumer,
    MigrationInProgress,
    Producer,
    Router,
    hash_key,
    partition_for,
)


class TestMapping(unittest.TestCase):
    def test_same_key_stable_while_n_unchanged(self):
        for n in (1, 3, 32, 64):
            first = partition_for("order-123", n)
            for _ in range(100):
                self.assertEqual(partition_for("order-123", n), first)

    def test_partition_in_range(self):
        for n in (1, 2, 7, 100):
            for i in range(200):
                p = partition_for("key-%d" % i, n)
                self.assertGreaterEqual(p, 0)
                self.assertLess(p, n)

    def test_invalid_partition_count(self):
        with self.assertRaises(ValueError):
            partition_for("k", 0)
        with self.assertRaises(ValueError):
            Router(-1)

    def test_empty_key_deterministic(self):
        # None, "" and b"" all map deterministically to the same partition.
        a = partition_for(None, 16)
        b = partition_for("", 16)
        c = partition_for(b"", 16)
        self.assertEqual(a, b)
        self.assertEqual(b, c)
        for _ in range(50):
            self.assertEqual(partition_for("", 16), a)

    def test_str_and_bytes_equivalent(self):
        self.assertEqual(partition_for("abc", 8), partition_for(b"abc", 8))

    def test_bad_key_type(self):
        with self.assertRaises(TypeError):
            partition_for(123, 8)

    def test_skewed_prefix_keys_spread(self):
        # Keys differing only in a numeric suffix must not pile onto one
        # partition: crc32 avalanche spreads them.
        n = 16
        counts = [0] * n
        for i in range(1600):
            counts[partition_for("user:%07d" % i, n)] += 1
        nonempty = sum(1 for c in counts if c > 0)
        self.assertGreaterEqual(nonempty, n - 1)
        self.assertLess(max(counts), 1600 // n * 2)


class TestSinglePartition(unittest.TestCase):
    def test_single_partition_keeps_global_order(self):
        broker = Broker(1)
        producer = Producer(broker)
        consumer = Consumer(broker)
        keys = ["a", "b", "c", None]
        rng = random.Random(1)
        sent = []
        for i in range(1000):
            key = rng.choice(keys)
            seq = producer.send(key, i)
            sent.append((key, seq))
        self.assertTrue(all(len(p) == 1000 for p in broker.partitions))
        consumer.drain()
        for key in keys:
            expected = [s for k, s in sent if k == key]
            self.assertEqual(consumer.delivered_sequence(key), expected)


class TestMigration(unittest.TestCase):
    def _run_dual_read(self, old_n, new_n, messages_per_phase, seed):
        rng = random.Random(seed)
        broker = Broker(old_n, mode="dual_read")
        producer = Producer(broker)
        consumer = Consumer(broker)
        keys = ["k%03d" % i for i in range(50)]
        produced = {k: [] for k in keys}

        def send_batch(tag):
            for _ in range(messages_per_phase):
                key = rng.choice(keys)
                produced[key].append(producer.send(key, tag))

        send_batch("before")
        consumer.poll(messages_per_phase // 2)
        broker.begin_migration(new_n)
        # writes keep flowing while consumer dual-reads old + new
        send_batch("during")
        consumer.poll(messages_per_phase // 2)
        send_batch("during")
        consumer.drain()
        self.assertTrue(broker.old_drained(consumer))
        broker.end_migration(consumer)
        send_batch("after")
        consumer.drain()
        for key in keys:
            self.assertEqual(
                consumer.delivered_sequence(key),
                produced[key],
                "key %r out of order during %d->%d migration"
                % (key, old_n, new_n),
            )
        return broker, producer, consumer

    def test_doubling_4_to_8(self):
        self._run_dual_read(4, 8, 500, seed=11)

    def test_doubling_32_to_64(self):
        self._run_dual_read(32, 64, 2000, seed=12)

    def test_shrink_8_to_3(self):
        self._run_dual_read(8, 3, 500, seed=13)

    def test_down_to_single_partition(self):
        self._run_dual_read(16, 1, 500, seed=14)

    def test_end_migration_requires_drain(self):
        broker = Broker(4)
        producer = Producer(broker)
        consumer = Consumer(broker)
        producer.send("k", "x")
        broker.begin_migration(8)
        with self.assertRaises(RuntimeError):
            broker.end_migration(consumer)
        consumer.drain()
        broker.end_migration(consumer)

    def test_reject_mode_blocks_writes_and_preserves_order(self):
        broker = Broker(4, mode="reject")
        producer = Producer(broker)
        consumer = Consumer(broker)
        for i in range(100):
            producer.send("k1", i)
        broker.begin_migration(8)
        with self.assertRaises(MigrationInProgress):
            producer.send("k1", "blocked")
        consumer.drain()
        broker.end_migration(consumer)
        for i in range(100, 200):
            producer.send("k1", i)
        consumer.drain()
        self.assertEqual(consumer.delivered_sequence("k1"),
                         list(range(1, 201)))
        self.assertEqual(len(broker.partitions), 8)

    def test_mapping_changes_when_n_changes(self):
        # Documents why migration is needed: mod-N mapping is only stable
        # for fixed N; doubling moves roughly half the keys.
        moved = sum(
            1
            for i in range(1000)
            if partition_for("k%04d" % i, 32)
            != partition_for("k%04d" % i, 64)
        )
        self.assertGreater(moved, 300)


class TestHotKey(unittest.TestCase):
    def test_hot_key_single_partition_ordered_others_spread(self):
        n = 32
        broker = Broker(n)
        producer = Producer(broker)
        consumer = Consumer(broker)
        hot = "HOT-KEY"
        hot_partition = partition_for(hot, n)
        for i in range(10000):
            producer.send(hot, i)
        for i in range(10000):
            producer.send("cold-%d" % (i % 500), i)
        consumer.drain()
        # hot key: all in one partition, strictly ordered
        self.assertEqual(consumer.delivered_sequence(hot),
                         list(range(1, 10001)))
        self.assertTrue(
            all(k != hot for k, _, _ in
                [m for p_i, p in enumerate(broker.partitions)
                 if p_i != hot_partition for m in p])
        )
        # cold keys still spread over many partitions
        used = sum(1 for p in broker.partitions if p)
        self.assertGreaterEqual(used, n - 2)

    def test_empty_key_goes_to_one_partition(self):
        n = 8
        broker = Broker(n)
        producer = Producer(broker)
        consumer = Consumer(broker)
        for i in range(100):
            producer.send(None, i)
        consumer.drain()
        self.assertEqual(consumer.delivered_sequence(None),
                         list(range(1, 101)))
        used = [i for i, p in enumerate(broker.partitions) if p]
        self.assertEqual(len(used), 1)
        # "" routes to the exact same partition as None
        self.assertEqual(partition_for("", n), used[0])


if __name__ == "__main__":
    unittest.main()
